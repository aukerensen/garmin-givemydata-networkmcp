"""Tests for scripts/compare_db.py on two small hand-made databases."""

import importlib.util
import sqlite3
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "compare_db", Path(__file__).resolve().parent.parent / "scripts" / "compare_db.py"
)
cd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cd)


def _make(path, rows, extra_table):
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE t (id INTEGER, k TEXT, v REAL, w TEXT, PRIMARY KEY (id, k))")
    c.execute("CREATE TABLE nopk (x)")
    c.execute(f"CREATE TABLE {extra_table} (x)")
    c.executemany("INSERT INTO t VALUES (?,?,?,?)", rows)
    c.execute("INSERT INTO nopk VALUES (1)")
    c.commit()
    c.close()


@pytest.fixture
def dbs(tmp_path):
    a, b = tmp_path / "a.db", tmp_path / "b.db"
    _make(a, [(1, "a", 1.0, None), (2, "a", None, "x"), (3, "a", 3.0, "y"), (4, "a", 4.0, "z")], "onlya")
    _make(b, [(1, "a", 1.0, "filled"), (2, "a", 2.5, None), (3, "a", 3.5, "y"), (5, "a", 5.0, "n")], "onlyb")
    return a, b


def _run(a, b, capsys, *extra):
    assert cd.main([str(a), str(b), *extra]) == 0
    return capsys.readouterr().out


def test_counts_and_changes(dbs, capsys):
    out = _run(*dbs, capsys)
    assert "Tables only in A: onlya" in out and "Tables only in B: onlyb" in out
    assert "== t: rows 4 -> 4 (+0)" in out
    assert "rows only in A: 1, only in B: 1" in out
    assert "v: NULL->value 1, value->NULL 0, value->other 1" in out
    assert "w: NULL->value 1, value->NULL 1, value->other 0" in out
    assert "[3,'a'] 3.0 -> 3.5" in out
    assert "no common primary key" in out  # nopk


def test_examples_limit_and_identical(dbs, tmp_path, capsys):
    a, b = dbs
    out = _run(a, b, capsys, "--examples", "0")
    assert "[3,'a']" not in out
    out_same = _run(a, a, capsys)
    assert "identical" in out_same


def test_read_only(dbs, capsys):
    a, b = dbs
    before = (a.stat().st_mtime_ns, b.stat().st_mtime_ns)
    _run(a, b, capsys)
    assert (a.stat().st_mtime_ns, b.stat().st_mtime_ns) == before
