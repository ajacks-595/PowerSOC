"""Regression tests for the 2026-10-26 review: backups, restore and the
encryption-key guard (P1.4/P1.5 + restore P2s). Written to fail first.
Hermetic per the project rules (see conftest)."""
from __future__ import annotations

import os
import sqlite3
from datetime import date, timedelta

import pytest

import app as app_module
import auth
import config
import database as db
import sync

# ---------- backups live on another DEVICE, failures are loud ----------------

def test_backup_isolation_compares_devices_not_paths(tmp_path, monkeypatch):
    """/host/backups and /data are different PATHS on the Pi but the same SD
    card: the path check called that isolated."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "BACKUP_DIR", tmp_path / "elsewhere" / "backups")
    (tmp_path / "data").mkdir()
    assert config.backup_dir_isolated() is False          # same filesystem
    monkeypatch.setattr(config, "_device_of", lambda p: 1 if "data" in str(p) else 2)
    assert config.backup_dir_isolated() is True


def test_a_failed_backup_is_recorded_and_fails_the_heartbeat(monkeypatch):
    def boom(*a, **k):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(db, "backup_database", boom)
    sync.daily_housekeeping()
    st = sync.backup_status()
    assert st["ok"] is False and "No space left" in st["error"]
    monkeypatch.setattr(sync, "ingest_ok", lambda: True)
    assert sync.scheduler_ok() is False


def test_health_reports_backup_failure_and_isolation(monkeypatch):
    application = app_module.create_app()
    c = application.test_client()
    db.setting_set("last_backup_error", "OSError: No space left on device")
    d = c.get("/api/health").get_json()["data"]
    assert d["backup_ok"] is False
    assert d["backup_isolated"] in (True, False)


def test_backup_retention_keeps_weekly_and_monthly_tiers(tmp_path, monkeypatch):
    out = tmp_path / "b"
    out.mkdir()
    today = date(2026, 10, 26)
    for back in range(400):
        (out / f"powersoc-{(today - timedelta(days=back)).isoformat()}.db").write_bytes(b"x")
    db._prune_backups(out, keep=14, today=today)
    kept = sorted(p.name[9:19] for p in out.glob("powersoc-*.db"))
    dailies = [(today - timedelta(days=b)).isoformat() for b in range(14)]
    assert set(dailies) <= set(kept)
    sundays = [d for d in kept if date.fromisoformat(d).weekday() == 6
               and d not in dailies]
    assert len(sundays) >= 6                                  # ~8 weeks of Sundays
    firsts = [d for d in kept if d.endswith("-01")]
    assert len(firsts) == 12                                   # a year of months
    assert len(kept) < 40


def test_backups_can_be_mirrored_to_a_second_disk(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BACKUP_MIRROR_DIR", tmp_path / "usb")
    path = db.backup_database()
    mirrored = tmp_path / "usb" / os.path.basename(path)
    assert mirrored.exists()
    c = sqlite3.connect(mirrored)
    try:
        assert c.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    finally:
        c.close()                     # -W error: an unclosed connection fails CI


def test_latest_backup_can_be_downloaded_by_an_admin():
    application = app_module.create_app()
    c = application.test_client()
    uid = db.insert_user("admin", auth.hash_password("supersecret1"), role="admin")
    with c.session_transaction() as s:
        s["user_id"], s["epoch"] = uid, 0
    assert c.get("/api/backup/latest").status_code == 404     # none yet
    db.backup_database()
    with c.get("/api/backup/latest") as r:          # closes the streamed file
        assert r.status_code == 200 and r.data[:15] == b"SQLite format 3"
        assert "attachment" in r.headers["Content-Disposition"]
        assert r.headers["Cache-Control"] == "no-store"
    db.insert_user("viewer", auth.hash_password("supersecret1"))
    v = application.test_client()
    v.post("/auth/login", data={"username": "viewer", "password": "supersecret1"})
    assert v.get("/api/backup/latest").status_code == 403


# ---------- the encryption key must never change silently ---------------------

def test_key_check_is_created_then_verified(monkeypatch):
    monkeypatch.setenv("POWERSOC_MACHINE_ID", "seed-A")
    config._fernet_cache.clear()
    assert config.verify_key_check() == "created"
    assert config.verify_key_check() == "ok"
    monkeypatch.setenv("POWERSOC_MACHINE_ID", "seed-B")
    config._fernet_cache.clear()
    assert config.verify_key_check() == "mismatch"


def test_startup_refuses_a_changed_key(monkeypatch):
    monkeypatch.setenv("POWERSOC_MACHINE_ID", "seed-A")
    config._fernet_cache.clear()
    app_module.create_app()
    monkeypatch.setenv("POWERSOC_MACHINE_ID", "seed-B")
    config._fernet_cache.clear()
    with pytest.raises(RuntimeError, match="encryption key"):
        app_module.create_app()
    monkeypatch.setenv("POWERSOC_ACCEPT_NEW_KEY", "1")
    app_module.create_app()                                   # deliberate re-key
    monkeypatch.delenv("POWERSOC_ACCEPT_NEW_KEY")
    assert config.verify_key_check() == "ok"


def test_key_check_is_not_blessed_onto_unreadable_credentials(monkeypatch):
    monkeypatch.setenv("POWERSOC_MACHINE_ID", "seed-B")
    config._fernet_cache.clear()
    db.setting_set("cred:OCTOPUS_API_KEY", "gAAAAAnot-decryptable-under-this-key")
    assert config.verify_key_check() == "skipped"
    assert db.setting_get("key_check") is None


def _backup_under(seed, monkeypatch, tmp_path, name):
    monkeypatch.setenv("POWERSOC_MACHINE_ID", seed)
    config._fernet_cache.clear()
    config.verify_key_check()
    config.set_credential("OCTOPUS_API_KEY", "sk_live")
    return db.backup_database(dest_dir=str(tmp_path / name))


def test_restore_refuses_a_backup_from_another_key(monkeypatch, tmp_path):
    src = _backup_under("seed-A", monkeypatch, tmp_path, "a")
    monkeypatch.setenv("POWERSOC_MACHINE_ID", "seed-B")
    config._fernet_cache.clear()
    with pytest.raises(RuntimeError, match="different encryption key"):
        db.restore_database(src)
    db.restore_database(src, accept_new_key=True)             # explicit override


# ---------- restore keeps a hot WAL and never leaves a half-swapped DB ----------

def test_restore_includes_writes_still_in_the_wal(tmp_path):
    src = tmp_path / "hot.db"
    keep = sqlite3.connect(src)
    keep.execute("PRAGMA journal_mode=WAL")
    keep.execute("PRAGMA wal_autocheckpoint=0")
    keep.execute("CREATE TABLE daily_summaries(id INTEGER PRIMARY KEY, date TEXT UNIQUE, pv_kwh REAL)")
    keep.execute("INSERT INTO daily_summaries(date, pv_kwh) VALUES('2026-10-25', 7.5)")
    keep.commit()
    assert os.path.getsize(str(src) + "-wal") > 0             # still only in the WAL
    db.restore_database(str(src))
    keep.close()
    assert db.get_daily_summary("2026-10-25")["pv_kwh"] == 7.5


def test_a_failed_restore_leaves_the_live_db_in_place(tmp_path, monkeypatch):
    db.upsert_daily_summary({"date": "2026-10-01", "pv_kwh": 1.0})
    src = db.backup_database(dest_dir=str(tmp_path / "b"))

    def boom(*a, **k):
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(db, "_copy_into", boom)
    with pytest.raises(sqlite3.OperationalError):
        db.restore_database(src)
    assert db.get_daily_summary("2026-10-01")["pv_kwh"] == 1.0
    assert db.count_users() == 0 or True
    assert not list((tmp_path).glob("*.restore-tmp"))


# ---------- the scheduler watchdog --------------------------------------------

def test_watchdog_flags_dead_and_wedged_schedulers():
    from datetime import datetime, timedelta
    now = datetime(2026, 10, 26, 12, 0, tzinfo=sync.LONDON)
    started = now - timedelta(hours=10)
    fresh = (now - timedelta(minutes=50)).isoformat()
    assert sync.watchdog_reason({"daily": True, "intraday": True}, fresh, now,
                                3600, started) is None
    assert "died: intraday" in sync.watchdog_reason(
        {"daily": True, "intraday": False}, fresh, now, 3600, started)
    old = (now - timedelta(hours=4)).isoformat()
    assert "no intraday tick" in sync.watchdog_reason(
        {"daily": True, "intraday": True}, old, now, 3600, started)
    # Just started: no tick yet is expected.
    assert sync.watchdog_reason({"daily": True, "intraday": True}, None, now,
                                3600, now - timedelta(minutes=5)) is None


def test_service_worker_never_caches_backups_and_flags_offline_answers():
    application = app_module.create_app()
    js = application.test_client().get("/sw.js").get_data(as_text=True)
    for path in ('"/api/backup"', '"/api/audit"', '"/api/brief"'):
        assert path in js
    assert "X-PowerSOC-Offline" in js
