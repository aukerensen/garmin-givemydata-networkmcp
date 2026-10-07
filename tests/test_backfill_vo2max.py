"""Tests for scripts/backfill_vo2max.py — mocked client, no network."""

import importlib.util
import sqlite3
from pathlib import Path

import pytest

from garmin_mcp.db import init_db

_spec = importlib.util.spec_from_file_location(
    "backfill_vo2max", Path(__file__).resolve().parent.parent / "scripts" / "backfill_vo2max.py"
)
bf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bf)


def _running(day, precise):
    return {
        "cycling": None,
        "generic": {"calendarDate": day, "vo2MaxPreciseValue": precise, "vo2MaxValue": round(precise)},
    }


class FakeClient:
    """Mimics GarminClient._fetch_batch for the three VO2max endpoints."""

    def __init__(self):
        self.calls = []

    def _fetch_batch(self, rest, gql):
        self.calls.append((dict(rest), dict(gql)))
        start = rest["vo2max_trend"].split("/")[-2]
        return {
            "vo2max_trend": {"status": 200, "data": [_running(start, 49.5)]},
            "gql_vo2max_running": {"status": 200, "data": {"data": {"vo2MaxScalar": [_running(start, 49.5)]}}},
            "gql_vo2max_cycling": {
                "status": 200,
                "data": {
                    "data": {
                        "vo2MaxScalar": [
                            {"cycling": {"calendarDate": start, "vo2MaxPreciseValue": 52.25}, "generic": None}
                        ]
                    }
                },
            },
        }


@pytest.fixture
def real_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    yield conn
    conn.close()


class TestDateBlocks:
    def test_blocks_are_at_most_31_days_and_contiguous(self):
        blocks = bf.date_blocks("2026-01-01", "2026-03-31")
        assert blocks[0] == ("2026-01-01", "2026-01-31")
        assert blocks[1] == ("2026-02-01", "2026-03-03")
        assert blocks[-1][1] == "2026-03-31"
        from datetime import date, timedelta

        for (s, e), (s2, _) in zip(blocks, blocks[1:]):
            assert (date.fromisoformat(e) - date.fromisoformat(s)).days + 1 <= 31
            assert date.fromisoformat(s2) == date.fromisoformat(e) + timedelta(days=1)

    def test_single_day(self):
        assert bf.date_blocks("2026-05-05", "2026-05-05") == [("2026-05-05", "2026-05-05")]

    def test_invalid(self):
        with pytest.raises(ValueError):
            bf.date_blocks("2026-02-01", "2026-01-01")
        with pytest.raises(ValueError):
            bf.date_blocks("2026-01-01", "2026-03-01", block_days=32)


class TestRunBackfill:
    def test_only_vo2max_endpoints_requested(self, real_db):
        client = FakeClient()
        bf.run_backfill(client, real_db, "2026-01-01", "2026-01-31", sleep=lambda s: None, out=lambda *a: None)
        rest, gql = client.calls[0]
        assert set(rest) == {"vo2max_trend"}
        assert set(gql) == {"vo2max_running", "vo2max_cycling"}

    def test_dry_run_writes_nothing_but_reports(self, real_db):
        lines = []
        result = bf.run_backfill(
            FakeClient(), real_db, "2026-01-01", "2026-01-31", dry_run=True, sleep=lambda s: None, out=lines.append
        )
        assert real_db.execute("SELECT COUNT(*) FROM vo2max").fetchone()[0] == 0
        assert result["summary"]["RUNNING"]["n"] == 1
        assert result["summary"]["CYCLING"]["vmax"] == pytest.approx(52.25)
        text = "\n".join(lines)
        assert "would write" in text
        assert "RUNNING: 1 rows" in text

    def test_real_run_writes_decimal_values(self, real_db):
        result = bf.run_backfill(
            FakeClient(), real_db, "2026-01-01", "2026-02-15", sleep=lambda s: None, out=lambda *a: None
        )
        rows = real_db.execute(
            "SELECT calendar_date, sport, value FROM vo2max ORDER BY calendar_date, sport"
        ).fetchall()
        assert [(r[0], r[1]) for r in rows] == [
            ("2026-01-01", "CYCLING"),
            ("2026-01-01", "RUNNING"),
            ("2026-02-01", "CYCLING"),
            ("2026-02-01", "RUNNING"),
        ]
        assert rows[1]["value"] == pytest.approx(49.5)
        assert rows[0]["value"] == pytest.approx(52.25)
        assert result["blocks"] == 2 and result["failed_blocks"] == 0

    def test_pause_between_blocks_only(self, real_db):
        pauses = []
        bf.run_backfill(
            FakeClient(), real_db, "2026-01-01", "2026-03-31", pause=7, sleep=pauses.append, out=lambda *a: None
        )
        assert pauses == [7, 7]  # 3 blocks -> 2 pauses

    def test_failed_block_counted(self, real_db):
        class Failing:
            def _fetch_batch(self, rest, gql):
                return {"vo2max_trend": {"status": 500, "data": None}}

        result = bf.run_backfill(
            Failing(), real_db, "2026-01-01", "2026-01-10", sleep=lambda s: None, out=lambda *a: None
        )
        assert result["failed_blocks"] == 1
        assert result["summary"] == {}


class TestSyncLock:
    def test_second_holder_is_refused(self, tmp_path):
        p = tmp_path / ".garmin-sync.lock"
        with bf.sync_lock(p):
            with pytest.raises(SystemExit):
                with bf.sync_lock(p):
                    pass
        with bf.sync_lock(p):  # released again
            pass

    def test_lock_path_env(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SYNC_LOCK_PATH", str(tmp_path / "x.lock"))
        assert bf.lock_path(tmp_path) == tmp_path / "x.lock"
        monkeypatch.delenv("SYNC_LOCK_PATH")
        assert bf.lock_path(tmp_path) == tmp_path / ".garmin-sync.lock"
