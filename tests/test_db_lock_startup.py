"""Startup must survive a database locked by a concurrent writer (e.g. a sync).

init_db() always takes the write lock. When garmin-mcp started while a sync was
writing, the single attempt failed with "database is locked" and the server
never came up. get_connection() now waits GARMIN_DB_BUSY_TIMEOUT seconds and
server._init_db_with_retry() retries until GARMIN_MCP_INIT_MAX_WAIT.
"""

import sqlite3
import threading
import time

import pytest

from garmin_mcp import db, server


def _hold_write_lock(path, seconds, ready):
    conn = sqlite3.connect(path)
    conn.execute("BEGIN IMMEDIATE")
    ready.set()
    time.sleep(seconds)
    conn.commit()
    conn.close()


def _locked_db(tmp_path, seconds):
    path = str(tmp_path / "garmin.db")
    conn = db.get_connection(path)
    db.init_db(conn)
    conn.close()
    ready = threading.Event()
    t = threading.Thread(target=_hold_write_lock, args=(path, seconds, ready))
    t.start()
    ready.wait(5)
    return path, t


def test_get_connection_waits_for_lock(tmp_path, monkeypatch):
    monkeypatch.setenv("GARMIN_DB_BUSY_TIMEOUT", "10")
    path, t = _locked_db(tmp_path, 1.0)
    start = time.monotonic()
    conn = db.get_connection(path)
    db.init_db(conn)
    conn.close()
    t.join()
    assert time.monotonic() - start >= 0.5


def test_short_busy_timeout_still_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("GARMIN_DB_BUSY_TIMEOUT", "0.1")
    path, t = _locked_db(tmp_path, 1.5)
    conn = db.get_connection(path)
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        db.init_db(conn)
    conn.close()
    t.join()


def test_init_retry_until_unlocked(monkeypatch):
    calls = {"n": 0}

    def flaky_init(conn):
        calls["n"] += 1
        if calls["n"] < 3:
            raise sqlite3.OperationalError("database is locked")

    sleeps = []
    monkeypatch.setattr(server, "init_db", flaky_init)
    monkeypatch.setattr(server, "get_connection", lambda: sqlite3.connect(":memory:"))
    server._init_db_with_retry(max_wait=100, retry_delay=1, sleep=sleeps.append)
    assert calls["n"] == 3
    assert sleeps == [1, 1]


def test_init_retry_gives_up_after_max_wait(monkeypatch):
    def locked(conn):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(server, "init_db", locked)
    monkeypatch.setattr(server, "get_connection", lambda: sqlite3.connect(":memory:"))
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        server._init_db_with_retry(max_wait=0, retry_delay=1, sleep=lambda s: None)


def test_init_other_error_not_retried(monkeypatch):
    def broken(conn):
        raise sqlite3.OperationalError("no such table: x")

    sleeps = []
    monkeypatch.setattr(server, "init_db", broken)
    monkeypatch.setattr(server, "get_connection", lambda: sqlite3.connect(":memory:"))
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        server._init_db_with_retry(max_wait=100, retry_delay=1, sleep=sleeps.append)
    assert sleeps == []
