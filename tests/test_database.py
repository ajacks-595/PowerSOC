"""Tests for the SQLite connection helper + schema init."""
from __future__ import annotations

import database as db


def test_init_sets_persistent_wal_and_conn_pragmas(tmp_db):
    # init_db sets WAL once (persistent on the file); conn() sets the
    # per-connection pragmas (foreign keys on, synchronous NORMAL).
    with db.conn() as c:
        assert c.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert c.execute("PRAGMA synchronous").fetchone()[0] == 1   # NORMAL


def test_many_sequential_connections_work(tmp_db):
    # The pattern the app uses: many short-lived connections in a row.
    for i in range(20):
        db.setting_set(f"k{i}", str(i))
    assert db.setting_get("k19") == "19"
    assert len(db.setting_keys_with_prefix("k")) == 20


def test_upsert_intervals_batched_and_idempotent(tmp_db):
    # Batched upsert (one transaction) must still be idempotent on interval_start
    # and return the row count. Re-running with a changed value updates in place.
    rows = [{"interval_start": f"2026-05-20T{h:02d}:00:00+01:00",
             "interval_end": f"2026-05-20T{h:02d}:30:00+01:00",
             "consumption_kwh": 1.0, "tariff_window": "day"} for h in range(5)]
    assert db.upsert_intervals("electricity_intervals", rows) == 5
    rows[0]["consumption_kwh"] = 9.9
    assert db.upsert_intervals("electricity_intervals", rows) == 5   # upsert, not insert
    stored = db.intervals_for_date("electricity_intervals", "2026-05-20")
    assert len(stored) == 5                       # still 5 (idempotent on key)
    assert stored[0]["consumption_kwh"] == 9.9    # value updated
    assert db.upsert_intervals("electricity_intervals", []) == 0   # empty no-op


def test_prune_sync_log(tmp_db):
    db.sync_log_add("2000-01-01T00:00:00", "sunsynk", "ok")        # ancient
    db.sync_log_add("2099-01-01T00:00:00", "octopus_elec", "ok")   # far future
    removed = db.prune_sync_log(keep_days=90)
    assert removed == 1
    rows = db.sync_log_recent()
    assert len(rows) == 1 and rows[0]["run_at"].startswith("2099")
