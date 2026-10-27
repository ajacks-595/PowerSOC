"""Regression tests for the 2026-06-10 repo-review fix batch.

Each test pins one reviewed-and-fixed behavior: typed client error boundaries,
the JSON-envelope error handlers, auth/session hardening, sync robustness, and
notification delivery semantics. Hermetic per the project rules (conftest
provides the fresh DB, env scrub and network kill switch).
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
import requests

import app as app_module
import auth
import database as db
import sync

LONDON = ZoneInfo("Europe/London")


@pytest.fixture
def client():
    application = app_module.create_app()
    application.config.update(TESTING=True)
    return application.test_client()


@pytest.fixture
def auth_client(client):
    r = client.post("/setup", data={"setup_token": auth.setup_token(), "username": "admin",
                                    "password": "supersecret1"})
    assert r.status_code in (302, 303)
    return client


# ---------- error boundaries: requests exceptions must be typed -----------

def test_api_live_degrades_on_connection_error(auth_client, monkeypatch):
    """A Sunsynk network failure must degrade /api/live (sunsynk_error in the
    payload), never 500 — observed live as raw tracebacks on 2026-06-06."""
    import sunsynk
    monkeypatch.setattr(
        app_module.config.Settings, "load",
        staticmethod(lambda: SimpleNamespace(
            sunsynk_configured=True, sunsynk_username="u",
            sunsynk_password="p", sunsynk_plant_id="1",
            sunsynk_inverter_sn="sn")))
    monkeypatch.setattr(
        sunsynk.SunsynkClient, "get_realtime",
        lambda self: (_ for _ in ()).throw(
            sunsynk.SunsynkConnectionError("network error: ConnectionError")))
    r = auth_client.get("/api/live")
    assert r.status_code == 200
    data = r.get_json()["data"]
    assert data["sunsynk"] is None
    assert "network error" in data["sunsynk_error"]


def test_sunsynk_get_wraps_connection_error(tmp_path):
    """Raw requests exceptions must not escape the client untyped."""
    import sunsynk

    class _BoomSession:
        headers = {}

        def get(self, *a, **k):
            raise requests.ConnectionError("dns down")

        def close(self):
            pass

    c = sunsynk.SunsynkClient(username="u", password="p", plant_id="1",
                              token_cache_path=str(tmp_path / "t.json"))
    c.session = _BoomSession()
    c._token, c._token_expiry = "tok", 9e12
    with pytest.raises(sunsynk.SunsynkError) as ei:
        c._get("api/v1/plants")
    assert isinstance(ei.value, sunsynk.SunsynkConnectionError)


def test_octopus_get_wraps_connection_error():
    import octopus

    class _BoomSession:
        headers = {}
        auth = None

        def get(self, *a, **k):
            raise requests.Timeout("slow")

        def close(self):
            pass

    c = octopus.OctopusClient("key", "A-1")
    c.session = _BoomSession()
    with pytest.raises(octopus.OctopusError) as ei:
        c._get("/accounts/A-1/")
    assert isinstance(ei.value, octopus.OctopusConnectionError)


def test_run_daily_degrades_on_connection_error(monkeypatch):
    """The daily sync must never abort on a network blip — the brief is posted
    every day, degraded (core convention)."""
    import config
    fake = SimpleNamespace(
        sunsynk_configured=True, octopus_configured=False,
        sunsynk_username="u", sunsynk_password="p", sunsynk_plant_id="1",
        sunsynk_inverter_sn="sn", gas_units="m3")
    monkeypatch.setattr(config.Settings, "load", staticmethod(lambda: fake))
    import sunsynk
    monkeypatch.setattr(sync.sunsynk, "SunsynkClient",
                        lambda *a, **k: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(
        sync, "sync_sunsynk_day",
        lambda c, d: (_ for _ in ()).throw(
            sunsynk.SunsynkConnectionError("network error")))
    result = sync.run_daily("2026-05-03")
    assert any("sunsynk" in e for e in result["errors"])
    # Nothing fetched -> no all-zero summary (2026-10-07 rule); the brief
    # still builds, degraded, from a live computation.
    assert result["no_data"] is True
    assert db.get_daily_summary("2026-05-03") is None
    import brief
    assert "Sunsynk data unavailable" in brief.build_daily_brief("2026-05-03")


# ---------- JSON envelope error handlers -----------------------------------

def test_unhandled_exception_returns_json_envelope(auth_client, monkeypatch):
    import routes.api
    monkeypatch.setattr(routes.api, "_build_day_payload",
                        lambda d: (_ for _ in ()).throw(RuntimeError("boom")))
    r = auth_client.get("/api/today")
    assert r.status_code == 500
    body = r.get_json()
    assert body["success"] is False and body["error"] == "internal error"


def test_405_returns_json_envelope(auth_client):
    # Authenticated client: the auth middleware (correctly) answers first for
    # anonymous requests, so the 405 path is only reachable once signed in.
    r = auth_client.post("/api/health")
    assert r.status_code == 405
    assert r.get_json()["success"] is False


def test_non_ascii_home_token_is_401_not_500(auth_client):
    auth_client.post("/api/settings/home-token/generate")
    r = auth_client.get("/api/home/status",
                        headers={"X-PowerSOC-Token": "café-token-\xff"})
    assert r.status_code == 401


def test_history_overflow_anchor_is_400(auth_client):
    r = auth_client.get("/api/history?period=week&anchor=9999-12-31")
    assert r.status_code == 400
    r = auth_client.get("/api/history?period=month&anchor=9999-12-01")
    assert r.status_code == 400


def test_day_date_is_canonicalised(auth_client):
    db.upsert_daily_summary({"date": "2026-12-31", "pv_kwh": 1.0})
    r = auth_client.get("/api/day/20261231")   # non-canonical ISO form
    assert r.status_code == 200
    d = r.get_json()["data"]
    assert d["date"] == "2026-12-31"
    assert d["summary"]["pv_kwh"] == 1.0       # finds the stored row


def test_backfill_start_invalid_date_is_400(auth_client):
    r = auth_client.post("/api/backfill/start", json={"start": "not-a-date"})
    assert r.status_code == 400
    assert auth_client.get(
        "/api/backfill/status").get_json()["data"]["running"] is False


def test_users_password_non_string_is_400_and_revokes(auth_client):
    r = auth_client.post("/api/users", json={"username": "bob",
                                             "password": "longenough1"})
    uid = r.get_json()["data"]["id"]
    assert auth_client.post(f"/api/users/{uid}/password",
                            json={"password": 12345678}).status_code == 400
    before = db.get_user(uid)["session_epoch"]
    r = auth_client.post(f"/api/users/{uid}/password",
                         json={"password": "newpassword1"})
    assert r.get_json()["data"]["sessions_revoked"] is True
    assert db.get_user(uid)["session_epoch"] == before + 1


def test_admin_revoke_sessions_endpoint(auth_client):
    uid = auth_client.post("/api/users", json={
        "username": "carol", "password": "longenough1"}).get_json()["data"]["id"]
    before = db.get_user(uid)["session_epoch"]
    r = auth_client.post(f"/api/users/{uid}/revoke-sessions")
    assert r.get_json()["data"]["revoked"] is True
    assert db.get_user(uid)["session_epoch"] == before + 1


# ---------- setup race + session epoch -------------------------------------

def test_insert_first_admin_is_atomic():
    assert db.insert_first_admin("a", "hash") is not None
    assert db.insert_first_admin("b", "hash") is None   # users exist -> refused


def test_setup_session_survives_revocation_correctly(client):
    """The /setup session must carry an epoch — one minted without it adopted
    the next epoch and survived 'revoke all sessions'."""
    client.post("/setup", data={"setup_token": auth.setup_token(), "username": "admin",
                                "password": "supersecret1"})
    uid = db.get_user_by_username("admin")["id"]
    # Bump the epoch out-of-band (as a stolen-cookie response would).
    db.bump_session_epoch(uid)
    r = client.get("/api/today")
    assert r.status_code == 401   # the setup session is invalidated


# ---------- TOTP replay + recovery codes -----------------------------------

def _make_totp_user():
    import totp
    secret = totp.generate_secret()
    uid = db.insert_user("tina", auth.hash_password("longenough1"))
    db.set_user_totp(uid, secret)
    return uid, secret


def test_totp_single_use(client):
    import totp
    uid, secret = _make_totp_user()
    code = totp._hotp(secret, int(__import__("time").time() // 30))
    row = db.get_user(uid)
    assert auth._second_factor_ok(row, code) is True
    row = db.get_user(uid)   # re-read: last counter now recorded
    assert auth._second_factor_ok(row, code) is False   # replay rejected


def test_recovery_code_consumed_once(client):
    import hashlib
    uid, _secret = _make_totp_user()
    codes = ["aabbccdd", "11223344"]
    db.set_recovery_codes(uid, json.dumps(
        [hashlib.sha256(c.encode()).hexdigest() for c in codes]))
    with client.application.test_request_context():
        row = db.get_user(uid)
        assert auth._consume_recovery_code(row, "aabbccdd") is True
        row = db.get_user(uid)
        assert auth._consume_recovery_code(row, "aabbccdd") is False  # used up
        assert auth._consume_recovery_code(row, "11223344") is True


def test_failed_login_is_audited(client):
    db.insert_user("admin", auth.hash_password("supersecret1"))
    client.post("/login", data={"username": "admin", "password": "wrong"})
    rows = db.audit_list(action="user.login_failed")
    assert rows and json.loads(rows[0]["details"])["reason"] == "bad_credentials"


def test_login_throttle_expires(client, monkeypatch):
    db.insert_user("admin", auth.hash_password("supersecret1"))
    t = {"now": 1_000_000.0}
    monkeypatch.setattr(auth.time, "time", lambda: t["now"])
    for _ in range(app_module.config.LOGIN_MAX_FAILURES):
        client.post("/login", data={"username": "admin", "password": "wrong"})
    r = client.post("/login", data={"username": "admin",
                                    "password": "supersecret1"})
    assert r.status_code == 429
    # Window elapses -> the same correct login now succeeds.
    t["now"] += app_module.config.LOGIN_WINDOW_SECONDS + 1
    r = client.post("/login", data={"username": "admin",
                                    "password": "supersecret1"})
    assert r.status_code in (302, 303)


# ---------- migrations on a populated legacy DB ----------------------------

def test_migration_upgrades_populated_legacy_db(tmp_path, monkeypatch):
    """The upgrade path every existing deployment takes: a pre-_MIGRATIONS
    schema with data must gain the new columns, twice, losslessly."""
    legacy = str(tmp_path / "legacy.db")
    c = sqlite3.connect(legacy)
    c.executescript("""
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'user',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_login_at TEXT,
            disabled INTEGER NOT NULL DEFAULT 0
        );
        INSERT INTO users(username, password_hash, role)
            VALUES('legacy-admin', 'pbkdf2$200000$abc$def', 'admin');
        CREATE TABLE daily_summaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL UNIQUE, pv_kwh REAL);
        INSERT INTO daily_summaries(date, pv_kwh) VALUES('2025-01-01', 9.9);
    """)
    c.close()
    monkeypatch.setattr(db, "DB_PATH", legacy)
    db.init_db()
    db.init_db()   # idempotent
    row = db.get_user_by_username("legacy-admin")
    assert row["password_hash"].startswith("pbkdf2$200000$")
    assert row["session_epoch"] == 0          # migrated column, default applied
    assert row["totp_last_counter"] is None
    assert row["recovery_codes"] is None
    assert db.get_daily_summary("2025-01-01")["pv_kwh"] == 9.9


# ---------- rate_at UTC normalisation --------------------------------------

def test_rate_at_handles_bst_offset_near_boundary():
    """A +01:00 London timestamp compared lexicographically against Z-suffixed
    UTC rows picked the wrong rate near boundaries — must be normalised."""
    db.upsert_tariff_rate("electricity_import", "2026-06-09T21:00:00Z", 10.0,
                          "2026-06-09T22:00:00Z")
    db.upsert_tariff_rate("electricity_import", "2026-06-09T22:00:00Z", 30.0,
                          None)
    # 22:30 BST = 21:30 UTC -> must match the 10p row, not the 30p one.
    row = db.rate_at("electricity_import", "2026-06-09T22:30:00+01:00")
    assert row["rate_p_per_kwh"] == 10.0
    # 23:30 BST = 22:30 UTC -> the 30p row.
    row = db.rate_at("electricity_import", "2026-06-09T23:30:00+01:00")
    assert row["rate_p_per_kwh"] == 30.0


# ---------- sync robustness -------------------------------------------------

def test_expected_intervals_dst_aware():
    assert sync.expected_intervals("2026-06-10") == 48
    assert sync.expected_intervals("2026-03-29") == 46   # spring forward
    assert sync.expected_intervals("2026-10-25") == 50   # fall back


def test_standing_charges_preserved_on_rerun():
    """A re-run during an Octopus blip must not NULL stored standing charges
    (silently understating net cost)."""
    db.upsert_daily_summary({"date": "2026-05-01", "standing_elec_pence": 60.0,
                             "standing_gas_pence": 30.0})
    s = sync.compute_daily_summary("2026-05-01")   # no charges passed
    assert s["standing_elec_pence"] == 60.0
    assert s["standing_gas_pence"] == 30.0


def test_already_stored_retries_failed_and_estimated_days():
    from datetime import date
    today = date(2026, 6, 10)
    d = date(2026, 5, 1)
    # No summary -> not stored.
    assert sync._already_stored(d, today, force=False) is False
    # All-zero failure summary -> must NOT count as stored.
    db.upsert_daily_summary({"date": "2026-05-01", "octopus_ok": 0,
                             "sunsynk_ok": 0, "data_partial": 0,
                             "import_estimated": 0})
    assert sync._already_stored(d, today, force=False) is False
    # Still carrying a CT estimate -> retry.
    db.upsert_daily_summary({"date": "2026-05-01", "octopus_ok": 1,
                             "sunsynk_ok": 1, "data_partial": 0,
                             "import_estimated": 1})
    assert sync._already_stored(d, today, force=False) is False
    # Genuinely settled (and priced) -> skip.
    db.upsert_daily_summary({"date": "2026-05-01", "octopus_ok": 1,
                             "sunsynk_ok": 1, "data_partial": 0,
                             "import_estimated": 0, "import_cost_pence": 100.0})
    assert sync._already_stored(d, today, force=False) is True
    assert sync._already_stored(d, today, force=True) is False


def test_backfill_survives_transient_sunsynk_failures(monkeypatch):
    """One 502 (ok=False) must not flip sunsynk_exhausted; only consecutive
    genuinely-empty days end the walk."""
    import config
    fake = SimpleNamespace(sunsynk_configured=True, octopus_configured=False,
                           sunsynk_username="u", sunsynk_password="p",
                           sunsynk_plant_id="1", sunsynk_inverter_sn="sn",
                           gas_units="m3")
    monkeypatch.setattr(config.Settings, "load", staticmethod(lambda: fake))
    monkeypatch.setattr(sync.sunsynk, "SunsynkClient",
                        lambda *a, **k: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(sync.time, "sleep", lambda s: None)
    calls = []

    def fake_day(client, ds):
        calls.append(ds)
        # Newest day fails transiently; everything older is fine.
        if len(calls) == 1:
            return 0, sync.sunsynk.DayResult(date=ds, ok=False)
        return 1, sync.sunsynk.DayResult(date=ds)

    monkeypatch.setattr(sync, "sync_sunsynk_day", fake_day)
    sync.run_backfill(max_days=6, force=True)
    assert len(calls) == 6   # the walk continued past the failure


def test_backfill_status_survives_restart():
    db.setting_set("backfill_state", json.dumps(
        {"running": True, "days_done": 5, "days_total": 10,
         "skipped": 1, "error": None}))
    st = sync.backfill_status()
    assert st["days_done"] == 5
    assert st["running"] is False   # a persisted 'running' is stale by definition


# ---------- notifications ---------------------------------------------------

def test_post_brief_never_raises_on_malformed_url(monkeypatch):
    import notifications
    # MissingSchema (mistyped webhook URL) escaped the old narrow except and
    # aborted the intraday tick before alerts ran. (Under the conftest network
    # kill switch the failure surfaces as ConnectionError — the property under
    # test is the same: any RequestException returns (False, detail) and the
    # secret hook path never leaks into the detail string.)
    # A scheme-less URL is now rejected by validation before any request
    # (2026-09-30); a well-formed URL that fails in transit still degrades.
    sent, detail = notifications.post_brief(
        "x", webhook_url="mattermost.example.com/hooks/secret", channel="c")
    assert sent is False
    assert "invalid webhook URL" in detail
    assert "hooks/secret" not in detail
    sent, detail = notifications.post_brief(
        "x", webhook_url="https://mattermost.example.com/hooks/secret", channel="c")
    assert sent is False
    assert "delivery failed" in detail
    assert "hooks/secret" not in detail


def test_post_brief_redacts_webhook_on_connection_error(monkeypatch):
    import notifications
    sent, detail = notifications.post_brief(
        "x", webhook_url="https://mm.example/hooks/secret-token", channel="c")
    assert sent is False
    assert "secret-token" not in detail   # ConnectionError text embeds the URL


def test_channel_hash_stripped_even_with_leading_space(monkeypatch):
    import notifications
    assert notifications._build_payload("t", " #town-square")["channel"] == \
        "town-square"


def test_alert_not_marked_fired_when_delivery_fails(monkeypatch):
    import alerts
    import config
    monkeypatch.setattr(config.Settings, "load", staticmethod(
        lambda: SimpleNamespace(mattermost_configured=True,
                                sunsynk_configured=True)))
    attempts = []
    results = [(False, "503"), (True, "ok")]
    monkeypatch.setattr(alerts.notifications, "post_brief",
                        lambda msg, **k: (attempts.append(msg),
                                          results.pop(0))[1])
    today = datetime.now(LONDON).date().isoformat()
    db.upsert_daily_summary({"date": today, "end_soc_pct": 5, "sunsynk_ok": 1,
                             "net_cost_pence": 0, "peak_import_kwh": 0})
    assert alerts.check_alerts(today) == []        # delivery failed -> retry
    assert alerts.check_alerts(today) == ["battery_low"]   # delivered now
    assert len(attempts) == 2


def test_alert_recovery_notification(monkeypatch):
    import alerts
    import config
    monkeypatch.setattr(config.Settings, "load", staticmethod(
        lambda: SimpleNamespace(mattermost_configured=True,
                                sunsynk_configured=True)))
    sent = []
    monkeypatch.setattr(alerts.notifications, "post_brief",
                        lambda msg, **k: (sent.append(msg), (True, "ok"))[1])
    today = datetime.now(LONDON).date().isoformat()
    db.upsert_daily_summary({"date": today, "end_soc_pct": 5, "sunsynk_ok": 1,
                             "net_cost_pence": 0, "peak_import_kwh": 0})
    assert alerts.check_alerts(today) == ["battery_low"]
    # Battery recovers (above threshold + hysteresis) -> one recovery note.
    db.upsert_daily_summary({"date": today, "end_soc_pct": 60, "sunsynk_ok": 1,
                             "net_cost_pence": 0, "peak_import_kwh": 0})
    assert alerts.check_alerts(today) == ["battery_recovered"]
    assert alerts.check_alerts(today) == []        # deduped
    assert len(sent) == 2


def test_alert_quiet_hours(monkeypatch):
    import alerts
    import config
    monkeypatch.setattr(config.Settings, "load", staticmethod(
        lambda: SimpleNamespace(mattermost_configured=True,
                                sunsynk_configured=True)))
    monkeypatch.setattr(config, "ALERT_QUIET_HOURS", "22-7", raising=False)
    sent = []
    monkeypatch.setattr(alerts.notifications, "post_brief",
                        lambda msg, **k: (sent.append(msg), (True, "ok"))[1])

    class _FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 6, 10, 23, 30, tzinfo=tz or LONDON)

    monkeypatch.setattr(alerts, "datetime", _FakeDT)
    db.upsert_daily_summary({"date": "2026-06-10", "end_soc_pct": 5,
                             "sunsynk_ok": 1, "net_cost_pence": 0,
                             "peak_import_kwh": 0})
    assert alerts.check_alerts("2026-06-10") == []   # suppressed, not marked
    assert sent == []
    # Same condition fires after the window ends.
    class _Morning(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 6, 10, 8, 0, tzinfo=tz or LONDON)

    monkeypatch.setattr(alerts, "datetime", _Morning)
    assert alerts.check_alerts("2026-06-10") == ["battery_low"]


def test_alert_degraded_streak(monkeypatch):
    import alerts
    import config
    monkeypatch.setattr(config.Settings, "load", staticmethod(
        lambda: SimpleNamespace(mattermost_configured=True,
                                sunsynk_configured=True)))
    sent = []
    monkeypatch.setattr(alerts.notifications, "post_brief",
                        lambda msg, **k: (sent.append(msg), (True, "ok"))[1])
    today = datetime.now(LONDON).date()
    for back in range(3):
        db.upsert_daily_summary({
            "date": (today - timedelta(days=back)).isoformat(),
            "sunsynk_ok": 0, "octopus_ok": 1, "end_soc_pct": None,
            "net_cost_pence": 0, "peak_import_kwh": 0})
    fired = alerts.check_alerts(today.isoformat())
    assert "sunsynk_streak" in fired


# ---------- brief additions -------------------------------------------------

def test_brief_month_digest_on_last_day():
    import brief
    for d in ("2026-04-29", "2026-04-30"):
        db.upsert_daily_summary({"date": d, "net_cost_pence": 100.0,
                                 "pv_kwh": 10.0, "sunsynk_ok": 1,
                                 "octopus_ok": 1})
    msg = brief.build_daily_brief("2026-04-30")
    assert "April digest" in msg
    msg = brief.build_daily_brief("2026-04-29")
    assert "digest" not in msg


def test_brief_week_context_line():
    import brief
    for back in range(1, 8):
        d = (datetime(2026, 5, 20) - timedelta(days=back)).date().isoformat()
        db.upsert_daily_summary({"date": d, "net_cost_pence": 100.0,
                                 "sunsynk_ok": 1, "octopus_ok": 1})
    db.upsert_daily_summary({"date": "2026-05-20", "net_cost_pence": 150.0,
                             "sunsynk_ok": 1, "octopus_ok": 1})
    msg = brief.build_daily_brief("2026-05-20")
    assert "vs 7-day avg" in msg and "50% above" in msg


def test_brief_unknown_costs_render_as_dash():
    import brief
    assert brief._money(None) == "–"
    assert brief._kwh(None) == "–"


# ---------- snapshot cache semantics ---------------------------------------

def test_cached_serves_stale_on_failure(monkeypatch):
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            return {"v": 1}
        raise RuntimeError("upstream down")

    with app_module._snap_meta_lock:
        app_module._snap_cache.clear()
    assert app_module._cached("k", ttl=0.0, fn=flaky) == {"v": 1}
    out = app_module._cached("k", ttl=0.0, fn=flaky)
    assert out["v"] == 1 and out["stale"] is True
    # Negative-cached: no third upstream call within _FAIL_TTL.
    out = app_module._cached("k", ttl=0.0, fn=flaky)
    assert calls["n"] == 2


def test_cached_negative_caches_first_failure():
    calls = {"n": 0}

    def always_fails():
        calls["n"] += 1
        raise RuntimeError("down")

    with app_module._snap_meta_lock:
        app_module._snap_cache.clear()
    with pytest.raises(RuntimeError):
        app_module._cached("k2", ttl=0.0, fn=always_fails)
    with pytest.raises(RuntimeError):
        app_module._cached("k2", ttl=0.0, fn=always_fails)
    assert calls["n"] == 1   # second raise came from the negative cache
