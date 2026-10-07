#!/usr/bin/env python3
"""Targeted historical backfill of the VO2max endpoints only.

The regular sync (``garmin-givemydata`` / ``garmin_sync()``) always walks every
endpoint (daily data, per-activity details, FIT files, ...). This script
fetches only ``vo2max_trend`` (REST maxmet/daily), ``vo2max_running`` and
``vo2max_cycling`` (GraphQL ``vo2MaxScalar``) for a date range, in blocks of at
most 31 days, and stores them through the normal ``save_to_db`` route
(``upsert_vo2max``). It reuses the package's own browser session / login and
the same cross-process lock file as the sync.

Usage:
    python scripts/backfill_vo2max.py --start 2025-01-01 --end 2026-10-07 --dry-run
    python scripts/backfill_vo2max.py --start 2025-01-01 --end 2026-10-07

With ``--dry-run`` nothing is written to the real database: the fetched data is
routed through the same code into a throwaway in-memory database, and the
summary shows what would be written. (It does still log in to Garmin and read
the three endpoints.)

Credentials come from GARMIN_EMAIL / GARMIN_PASSWORD (environment or
``$GARMIN_DATA_DIR/.env``), exactly like the CLI. The lock file is
``$SYNC_LOCK_PATH`` if set, else ``<data dir>/.garmin-sync.lock``.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from garmin_client.endpoints import full_range_graphql, full_range_rest  # noqa: E402
from garmin_mcp.db import get_connection, init_db, save_to_db  # noqa: E402

# Endpoint keys as used by full_range_rest() / full_range_graphql().
REST_ENDPOINTS = ("vo2max_trend",)
GQL_ENDPOINTS = ("vo2max_running", "vo2max_cycling")
MAX_BLOCK_DAYS = 31


def date_blocks(start: str, end: str, block_days: int = MAX_BLOCK_DAYS) -> list[tuple[str, str]]:
    """Split [start, end] (inclusive) into consecutive blocks of <= block_days days."""
    if not 1 <= block_days <= MAX_BLOCK_DAYS:
        raise ValueError(f"block_days must be between 1 and {MAX_BLOCK_DAYS}")
    s, e = date.fromisoformat(start), date.fromisoformat(end)
    if s > e:
        raise ValueError(f"start ({start}) is after end ({end})")
    blocks = []
    while s <= e:
        b_end = min(s + timedelta(days=block_days - 1), e)
        blocks.append((s.isoformat(), b_end.isoformat()))
        s = b_end + timedelta(days=1)
    return blocks


def lock_path(data_dir: Path | None = None) -> Path:
    env = os.environ.get("SYNC_LOCK_PATH")
    if env:
        return Path(env)
    if data_dir is None:
        from garmin_mcp.db import DB_PATH

        data_dir = Path(DB_PATH).parent
    return Path(data_dir) / ".garmin-sync.lock"


@contextlib.contextmanager
def sync_lock(path: Path):
    """Non-blocking exclusive flock on the shared sync lock file (same as the sync uses)."""
    f = open(path, "a")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        raise SystemExit("Another sync process holds the lock; aborting.")
    try:
        yield
    finally:
        fcntl.flock(f, fcntl.LOCK_UN)
        f.close()


def fetch_block(client, start: str, end: str) -> dict:
    """Fetch only the VO2max endpoints for one block. Returns {name: {"status", "data"}}."""
    rest = full_range_rest(None, start, end)
    gql = full_range_graphql(None, start, end)
    rest = {k: rest[k] for k in REST_ENDPOINTS}
    gql = {k: gql[k] for k in GQL_ENDPOINTS}
    return client._fetch_batch(rest, gql) or {}


def store_results(conn, results: dict) -> dict[str, int]:
    """Route fetched results through save_to_db. Returns {endpoint: records handed over}."""
    counts: dict[str, int] = {}
    for name, res in results.items():
        if not isinstance(res, dict) or res.get("status") != 200 or not res.get("data"):
            continue
        counts[name] = save_to_db(conn, name, res["data"])
    return counts


def summarize(conn, start: str, end: str) -> dict:
    """Per-sport row count, first/last date and min/max value for rows in [start, end]."""
    rows = conn.execute(
        """SELECT sport, COUNT(*) AS n, MIN(calendar_date) AS first, MAX(calendar_date) AS last,
                  MIN(value) AS vmin, MAX(value) AS vmax
           FROM vo2max WHERE calendar_date BETWEEN ? AND ? GROUP BY sport ORDER BY sport""",
        (start, end),
    ).fetchall()
    return {r["sport"]: {k: r[k] for k in ("n", "first", "last", "vmin", "vmax")} for r in rows}


def run_backfill(
    client,
    conn,
    start: str,
    end: str,
    dry_run: bool = False,
    pause: float = 3.0,
    block_days: int = MAX_BLOCK_DAYS,
    sleep=time.sleep,
    out=print,
) -> dict:
    """Fetch the VO2max endpoints block by block and store them.

    The data is always routed into a throwaway in-memory staging database too, so
    the summary (and the dry-run output) reflects what ``upsert_vo2max`` really
    accepts, not just what Garmin returned. ``conn`` is only written when not dry_run.
    """
    staging = get_connection(":memory:")
    init_db(staging)
    blocks = date_blocks(start, end, block_days)
    failed_blocks = 0
    try:
        for i, (b_start, b_end) in enumerate(blocks, 1):
            results = fetch_block(client, b_start, b_end)
            statuses = {n: (r.get("status") if isinstance(r, dict) else "?") for n, r in results.items()}
            if not results or all(s != 200 for s in statuses.values()):
                failed_blocks += 1
            counts = store_results(staging, results)
            if not dry_run:
                store_results(conn, results)
                conn.commit()
            out(f"block {i}/{len(blocks)} {b_start}..{b_end}: status={statuses} records={counts}")
            if i < len(blocks):
                sleep(pause)
        summary = summarize(staging, start, end)
    finally:
        staging.close()

    verb = "would write" if dry_run else "wrote"
    out(f"\nSummary ({verb}), {start}..{end}, {len(blocks)} block(s), {failed_blocks} failed:")
    if not summary:
        out("  no VO2max rows")
    for sport, s in summary.items():
        out(f"  {sport}: {s['n']} rows, {s['first']}..{s['last']}, value min {s['vmin']} max {s['vmax']}")
    return {"summary": summary, "blocks": len(blocks), "failed_blocks": failed_blocks, "dry_run": dry_run}


def _parse_args(argv=None):
    p = argparse.ArgumentParser(description="Backfill only the VO2max endpoints for a date range.")
    p.add_argument("--start", required=True, help="YYYY-MM-DD (inclusive)")
    p.add_argument("--end", required=True, help="YYYY-MM-DD (inclusive)")
    p.add_argument("--dry-run", action="store_true", help="Do not write to the real database")
    p.add_argument("--pause", type=float, default=3.0, help="Seconds between blocks (default 3)")
    p.add_argument("--block-days", type=int, default=MAX_BLOCK_DAYS, help="Days per block (<=31)")
    p.add_argument("--visible", action="store_true", help="Show the browser window")
    args = p.parse_args(argv)
    date.fromisoformat(args.start)
    date.fromisoformat(args.end)
    return args


def main(argv=None) -> int:
    args = _parse_args(argv)
    # Imported here: garmin_givemydata chdirs into the data dir on import.
    from garmin_client import GarminClient
    from garmin_givemydata import DATA_DIR, PROFILE_DIR, SESSION_FILE, load_env

    load_env()
    email, password = os.environ.get("GARMIN_EMAIL", ""), os.environ.get("GARMIN_PASSWORD", "")
    if not email or not password:
        print("GARMIN_EMAIL / GARMIN_PASSWORD not set.")
        return 1

    with sync_lock(lock_path(DATA_DIR)):
        conn = get_connection()
        init_db(conn)
        client = GarminClient(
            email=email,
            password=password,
            profile_dir=PROFILE_DIR,
            headless=not args.visible,
            session_file=SESSION_FILE,
        )
        try:
            if not client.login():
                print("Login failed!")
                return 1
            result = run_backfill(
                client,
                conn,
                args.start,
                args.end,
                dry_run=args.dry_run,
                pause=args.pause,
                block_days=args.block_days,
            )
        finally:
            client.close()
            conn.close()
    return 2 if result["failed_blocks"] else 0


if __name__ == "__main__":
    sys.exit(main())
