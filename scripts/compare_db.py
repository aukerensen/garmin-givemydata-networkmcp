#!/usr/bin/env python3
"""Compare two SQLite databases (e.g. garmin.db before/after running migrations).

For every table: row counts in both files. For tables with a primary key that
exist in both files: rows only in A / only in B, and per column the number of
rows (matched on the primary key) whose value changed, split in
NULL -> value, value -> NULL and value -> other value, with up to 5 examples
per column. Output is plain text.

Efficient on large files: both databases are ATTACHed read-only and compared in
SQL (one join per table and chunk of columns); nothing is loaded into Python
memory except the counts and a handful of example rows.

Usage:
    python scripts/compare_db.py ORIGINAL.db MODIFIED.db [--examples 5] [--tables a,b]
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

COLUMN_CHUNK = 150  # columns compared per join pass (3 aggregates each)
MAX_EXAMPLE_LEN = 80


def q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def connect(path_a: str, path_b: str) -> sqlite3.Connection:
    for p in (path_a, path_b):
        if not Path(p).is_file():
            raise SystemExit(f"Not a file: {p}")
    conn = sqlite3.connect(":memory:", uri=True)
    conn.execute("ATTACH DATABASE ? AS a", (f"file:{Path(path_a).resolve()}?mode=ro",))
    conn.execute("ATTACH DATABASE ? AS b", (f"file:{Path(path_b).resolve()}?mode=ro",))
    return conn


def tables(conn, schema: str) -> list[str]:
    rows = conn.execute(
        f"SELECT name FROM {schema}.sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [r[0] for r in rows]


def table_info(conn, schema: str, table: str) -> list[tuple[str, int]]:
    """[(column, pk_position)] in column order."""
    return [(r[1], r[5]) for r in conn.execute(f"PRAGMA {schema}.table_info({q(table)})")]


def short(v) -> str:
    if v is None:
        return "NULL"
    s = repr(v)
    return s if len(s) <= MAX_EXAMPLE_LEN else s[: MAX_EXAMPLE_LEN - 3] + "..."


def compare_table(conn, table: str, examples: int, out) -> None:
    a_cols, b_cols = table_info(conn, "a", table), table_info(conn, "b", table)
    n_a = conn.execute(f"SELECT COUNT(*) FROM a.{q(table)}").fetchone()[0]
    n_b = conn.execute(f"SELECT COUNT(*) FROM b.{q(table)}").fetchone()[0]
    delta = n_b - n_a
    out(f"\n== {table}: rows {n_a} -> {n_b} ({delta:+d})")

    a_names, b_names = [c for c, _ in a_cols], [c for c, _ in b_cols]
    if a_names != b_names:
        added = [c for c in b_names if c not in a_names]
        removed = [c for c in a_names if c not in b_names]
        if added:
            out(f"   columns only in B: {', '.join(added)}")
        if removed:
            out(f"   columns only in A: {', '.join(removed)}")

    pk = [c for c, p in sorted(a_cols, key=lambda x: x[1]) if p > 0]
    pk_b = [c for c, p in sorted(b_cols, key=lambda x: x[1]) if p > 0]
    if not pk or pk != pk_b:
        out("   (no common primary key: only row counts compared)")
        return

    on = " AND ".join(f"x.{q(c)} IS y.{q(c)}" for c in pk)
    first_pk = q(pk[0])
    only_a = conn.execute(
        f"SELECT COUNT(*) FROM a.{q(table)} x WHERE NOT EXISTS (SELECT 1 FROM b.{q(table)} y WHERE {on})"
    ).fetchone()[0]
    only_b = conn.execute(
        f"SELECT COUNT(*) FROM b.{q(table)} y WHERE NOT EXISTS (SELECT 1 FROM a.{q(table)} x WHERE {on})"
    ).fetchone()[0]
    out(f"   rows only in A: {only_a}, only in B: {only_b}")

    common = [c for c in a_names if c in b_names and c not in pk]
    changed_any = False
    for i in range(0, len(common), COLUMN_CHUNK):
        chunk = common[i : i + COLUMN_CHUNK]
        sel = []
        for c in chunk:
            x, y = f"x.{q(c)}", f"y.{q(c)}"
            sel.append(f"SUM({x} IS NULL AND {y} IS NOT NULL)")
            sel.append(f"SUM({x} IS NOT NULL AND {y} IS NULL)")
            sel.append(f"SUM({x} IS NOT NULL AND {y} IS NOT NULL AND {x} IS NOT {y})")
        row = conn.execute(f"SELECT {', '.join(sel)} FROM a.{q(table)} x JOIN b.{q(table)} y ON {on}").fetchone()
        for j, c in enumerate(chunk):
            n_null_val, n_val_null, n_val_val = (v or 0 for v in row[3 * j : 3 * j + 3])
            if not (n_null_val or n_val_null or n_val_val):
                continue
            changed_any = True
            out(f"   {c}: NULL->value {n_null_val}, value->NULL {n_val_null}, value->other {n_val_val}")
            if examples <= 0:
                continue
            x, y = f"x.{q(c)}", f"y.{q(c)}"
            pk_sel = ", ".join(f"x.{q(k)}" for k in pk)
            ex = conn.execute(
                f"SELECT {pk_sel}, {x}, {y} FROM a.{q(table)} x JOIN b.{q(table)} y ON {on} "
                f"WHERE {x} IS NOT {y} LIMIT ?",
                (examples,),
            ).fetchall()
            for r in ex:
                key = ",".join(short(v) for v in r[: len(pk)])
                out(f"      [{key}] {short(r[-2])} -> {short(r[-1])}")
    if not changed_any and not only_a and not only_b:
        out("   identical")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("db_a", help="original database")
    ap.add_argument("db_b", help="modified database")
    ap.add_argument("--examples", type=int, default=5, help="examples per column (default 5)")
    ap.add_argument("--changed-only", action="store_true", help="hide tables that are identical")
    ap.add_argument("--tables", help="comma-separated table names to restrict to")
    args = ap.parse_args(argv)

    conn = connect(args.db_a, args.db_b)
    ta, tb = tables(conn, "a"), tables(conn, "b")
    wanted = set(args.tables.split(",")) if args.tables else None
    print(f"A: {args.db_a}\nB: {args.db_b}")
    only_a, only_b = [t for t in ta if t not in tb], [t for t in tb if t not in ta]
    if only_a:
        print(f"Tables only in A: {', '.join(only_a)}")
    if only_b:
        print(f"Tables only in B: {', '.join(only_b)}")
    for t in [t for t in ta if t in tb and (wanted is None or t in wanted)]:
        lines: list[str] = []
        compare_table(conn, t, args.examples, lines.append)
        if args.changed_only and lines[-1].strip() == "identical":
            continue
        print("\n".join(lines))
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
