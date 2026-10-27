"""Regression tests for the 2026-09-30 repo-review fix batch.

One test (or a small group) per reviewed-and-fixed behaviour: off-volume
backups + restore, DST-safe scheduling, memoised key derivation, the .env
loader, first-run setup token, input-shape hardening (non-ASCII, non-finite,
wrong JSON type), encrypted TOTP secrets, upstream call budgets, live tariff
rates, client robustness, caching headers and housekeeping. Hermetic per the
project rules (conftest: fresh DB, env scrub, network kill switch).
"""
from __future__ import annotations

import collections
import json
import os
import sqlite3
import stat
import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
import requests

import app as app_module
import auth
import config
import database as db
import octopus
import sunsynk
import sync
import totp

LONDON = ZoneInfo("Europe/London")


@pytest.fixture
def client():
    application = app_module.create_app()
    application.config.update(TESTING=True)
    return application.test_client()


@pytest.fixture
def auth_client(client):
    r = client.post("/setup", data={"setup_token": auth.setup_token(),
                                    "username": "admin", "password": "supersecret1"})
    assert r.status_code in (302, 303)
    return client


def _strict_json(resp):
    """Parse a response body rejecting NaN/Infinity (what browsers' JSON does)."""
    def bad(c):
        raise ValueError(f"non-strict JSON constant {c}")
    return json.loads(resp.get_data(as_text=True), parse_constant=bad)


# ---------- P0-2: backups outside the data dir + restore ------------------

def test_backup_goes_to_backup_dir_verified_and_private(tmp_path, monkeypatch):
    bdir = tmp_path / "offvolume"
    monkeypatch.setattr(config, "BACKUP_DIR", bdir)
    db.setting_set("probe", "x")
    path = db.backup_database()
    assert path.startswith(str(bdir))
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode & 0o007 == 0 and mode & 0o022 == 0           # no world/group write
    c = sqlite3.connect(path)
    assert c.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    assert c.execute("SELECT value_encrypted FROM settings WHERE key='probe'"
                     ).fetchone()[0] == "x"
    c.close()
    assert db.setting_get("last_backup_at")
    assert not list(bdir.glob("*.tmp"))                      # no torn temp left


def test_backup_prunes_to_keep(tmp_path, monkeypatch):
    bdir = tmp_path / "b"
    bdir.mkdir()
    for d in ("2020-01-01", "2020-01-02", "2020-01-03"):
        (bdir / f"powersoc-{d}.db").write_bytes(b"old")
    monkeypatch.setattr(config, "BACKUP_DIR", bdir)
    db.backup_database(keep=2)
    names = sorted(p.name for p in bdir.glob("powersoc-*.db"))
    assert len(names) == 2 and names[0] == "powersoc-2020-01-03.db"


def test_backup_dir_isolation_check(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "BACKUP_DIR", tmp_path / "data" / "backups")
    assert config.backup_dir_isolated() is False
    monkeypatch.setattr(config, "BACKUP_DIR", tmp_path / "elsewhere")
    # A different path on the SAME filesystem is not isolated (2026-10-26:
    # judged by device); a different device is.
    assert config.backup_dir_isolated() is False
    monkeypatch.setattr(config, "_device_of",
                        lambda p: 2 if "elsewhere" in str(p) else 1)
    assert config.backup_dir_isolated() is True


def test_backup_restore_round_trip_keeps_users_and_credentials(tmp_path, monkeypatch):
    """The disaster-recovery path: with the same machine id, a restored backup
    brings back users AND decryptable credentials."""
    monkeypatch.setenv("POWERSOC_MACHINE_ID", "host-side-secret-1")
    monkeypatch.setattr(config, "BACKUP_DIR", tmp_path / "b")
    db.insert_user("alex", auth.hash_password("supersecret1"), role="admin")
    config.set_credential("OCTOPUS_API_KEY", "sk_live_abc")
    backup = db.backup_database()

    db.delete_user(db.get_user_by_username("alex")["id"])   # "disaster"
    config.set_credential("OCTOPUS_API_KEY", None)

    moved = db.restore_database(backup)
    assert moved and os.path.exists(moved)                    # old DB kept aside
    assert db.get_user_by_username("alex") is not None
    assert config.get_credential("OCTOPUS_API_KEY") == "sk_live_abc"


def test_restore_from_read_only_dir_and_wal_mode_source(tmp_path):
    """Live failure 2026-10-02: the prod backup was a WAL-mode file in a dir
    the container may only traverse — SQLite couldn't create the -shm file."""
    src_dir = tmp_path / "ro"
    src_dir.mkdir()
    src = src_dir / "prod.db"
    c = sqlite3.connect(src)
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(db.SCHEMA)
    c.execute("INSERT INTO users(username, password_hash) VALUES('old','x')")
    c.commit()
    c.close()
    os.chmod(src_dir, 0o555)
    try:
        db.restore_database(str(src))
    finally:
        os.chmod(src_dir, 0o755)
    assert db.get_user_by_username("old") is not None


def test_backups_are_standalone_rollback_journal_files(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BACKUP_DIR", tmp_path / "b")
    path = db.backup_database()
    c = sqlite3.connect(path)
    assert c.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    c.close()


def test_restore_refuses_damaged_backup(tmp_path):
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"not a database at all" * 100)
    with pytest.raises((RuntimeError, sqlite3.DatabaseError)):
        db.restore_database(str(bad))


def test_daily_housekeeping_backs_up_and_prunes(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BACKUP_DIR", tmp_path / "b")
    with db.conn() as c:
        c.execute("INSERT INTO audit_log(action, created_at) "
                  "VALUES('user.login_failed', '2020-01-01 00:00:00')")
        c.execute("INSERT INTO audit_log(action, created_at) "
                  "VALUES('user.login', '2020-01-01 00:00:00')")
    sync.daily_housekeeping()
    assert list((tmp_path / "b").glob("powersoc-*.db"))
    with db.conn() as c:
        actions = [r[0] for r in c.execute("SELECT action FROM audit_log")]
    assert "user.login_failed" not in actions     # attacker-driven rows bounded
    assert "user.login" in actions                # security record kept


# ---------- machine id / Fernet --------------------------------------------

def test_machine_id_file_is_used_and_fails_closed(tmp_path, monkeypatch):
    monkeypatch.delenv("POWERSOC_MACHINE_ID", raising=False)
    f = tmp_path / "machine_id"
    f.write_text("from-a-host-file\n")
    monkeypatch.setenv("POWERSOC_MACHINE_ID_FILE", str(f))
    assert config._machine_id() == "from-a-host-file"
    monkeypatch.setenv("POWERSOC_MACHINE_ID_FILE", str(tmp_path / "missing"))
    with pytest.raises(RuntimeError):
        config._machine_id()


def test_instance_id_created_private_and_reused(tmp_path, monkeypatch):
    monkeypatch.delenv("POWERSOC_MACHINE_ID", raising=False)
    monkeypatch.delenv("POWERSOC_MACHINE_ID_FILE", raising=False)
    monkeypatch.setattr(config, "_MACHINE_ID_PATHS", ())
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    first = config._machine_id()
    idf = tmp_path / ".instance_id"
    assert stat.S_IMODE(os.stat(idf).st_mode) == 0o600
    assert config._machine_id() == first
    idf.write_text("")                              # empty file gets filled
    assert config._machine_id() and idf.read_text()


def test_fernet_derived_once_not_per_call(monkeypatch):
    """Settings.load() used to run PBKDF2(200k) 14× (374 ms)."""
    for k in config.CREDENTIAL_KEYS:
        config.set_credential(k, "v-" + k)
    calls = collections.Counter()
    real = config.pbkdf2_hmac

    def counting(*a, **k):
        calls["n"] += 1
        return real(*a, **k)
    monkeypatch.setattr(config, "pbkdf2_hmac", counting)
    config._fernet_cache.clear()
    s = config.Settings.load()
    config.Settings.load()
    assert s.octopus_api_key == "v-OCTOPUS_API_KEY"
    assert calls["n"] <= 1


def test_fernet_cache_follows_machine_id_change(monkeypatch):
    monkeypatch.setenv("POWERSOC_MACHINE_ID", "id-A")
    tok = config.encrypt("secret")
    monkeypatch.setenv("POWERSOC_MACHINE_ID", "id-B")
    assert config.decrypt(tok) is None               # different key now
    monkeypatch.setenv("POWERSOC_MACHINE_ID", "id-A")
    assert config.decrypt(tok) == "secret"


# ---------- .env loader -----------------------------------------------------

def test_dotenv_inline_comments_quotes_and_empty(tmp_path, monkeypatch):
    keys = ["A1", "A2", "A3", "A4", "A5", "A6", "A7"]
    for k in keys:
        monkeypatch.delenv(k, raising=False)
    f = tmp_path / ".env"
    f.write_text("\n".join([
        "A1=value   # trailing comment",
        "A2=        # only a comment",
        "A3=",
        'A4="has # inside quotes"   # and a comment',
        "A5=http://x/#frag",
        "export A6=exported",
        "A7='single'",
    ]))
    config.load_dotenv_file(f)
    assert os.environ["A1"] == "value"
    assert "A2" not in os.environ and "A3" not in os.environ   # blank = unset
    assert os.environ["A4"] == "has # inside quotes"
    assert os.environ["A5"] == "http://x/#frag"                # no space before #
    assert os.environ["A6"] == "exported"
    assert os.environ["A7"] == "single"


def test_env_example_round_trips_through_our_loader(monkeypatch):
    """Copying .env.example → .env must never turn comments into values (it
    used to make POWERSOC_MACHINE_ID a string published in this repo)."""
    path = os.path.join(os.path.dirname(config.__file__), ".env.example")
    with open(path) as fh:
        keys = [ln.split("=", 1)[0].strip() for ln in fh
                if "=" in ln and not ln.lstrip().startswith("#")]
    for k in keys:
        monkeypatch.delenv(k, raising=False)
    config.load_dotenv_file(path)
    for k in keys:
        v = os.environ.get(k)
        assert v is None or not v.startswith("#"), (k, v)
    assert "POWERSOC_MACHINE_ID" not in os.environ
    assert "POWERSOC_DB_PATH" not in os.environ


# ---------- P2-6: DST-safe scheduler ---------------------------------------

@pytest.mark.parametrize("start", [datetime(2026, 10, 22, 7, 0, tzinfo=LONDON),
                                   datetime(2027, 3, 25, 7, 0, tzinfo=LONDON)])
def test_master_runs_once_per_local_day_at_0625_across_dst(start):
    now, runs = start, []
    for _ in range(7):
        wake = datetime.fromtimestamp(
            now.timestamp() + sync._seconds_until(now, 6, 25), LONDON)
        runs.append(wake)
        now = datetime.fromtimestamp(wake.timestamp() + 60, LONDON)
    assert all((w.hour, w.minute) == (6, 25) for w in runs), runs
    days = [w.date() for w in runs]
    assert days == sorted(set(days)) and len(days) == 7      # one per day


# ---------- P2-7: first-run setup token ------------------------------------

def test_setup_requires_the_one_time_token(client):
    tok = auth.setup_token()
    assert tok
    r = client.post("/setup", json={"username": "x", "password": "supersecret1"})
    assert r.status_code == 403
    r = client.post("/setup", json={"setup_token": "wrong", "username": "x",
                                    "password": "supersecret1"})
    assert r.status_code == 403
    assert db.count_users() == 0
    r = client.post("/setup", json={"setup_token": tok, "username": "x",
                                    "password": "supersecret1"})
    assert r.status_code == 200 and r.get_json()["success"]
    assert auth.setup_token() is None                       # gone once used
    assert db.setting_get("setup_token") is None


def test_setup_token_guessing_is_throttled(client, monkeypatch):
    monkeypatch.setattr(config, "LOGIN_MAX_FAILURES", 3)
    for _ in range(3):
        client.post("/setup", json={"setup_token": "nope", "username": "x",
                                    "password": "supersecret1"})
    r = client.post("/setup", json={"setup_token": auth.setup_token(),
                                    "username": "x", "password": "supersecret1"})
    assert r.status_code == 429 and db.count_users() == 0


def test_setup_token_preshared_via_env(client, monkeypatch):
    monkeypatch.setattr(config, "SETUP_TOKEN", "pre-shared-123")
    assert auth.setup_token() == "pre-shared-123"
    r = client.post("/setup", json={"setup_token": "pre-shared-123",
                                    "username": "x", "password": "supersecret1"})
    assert r.status_code == 200


def test_setup_rejects_non_object_json(client):
    r = client.post("/setup", json=["a", "b"])
    assert r.status_code == 400


def test_setup_page_shows_token_field(client):
    html = client.get("/setup").get_data(as_text=True)
    assert 'id="su-token"' in html


# ---------- P3: input-shape hardening ---------------------------------------

def _mutating_rules(application):
    for rule in application.url_map.iter_rules():
        for m in ("POST", "DELETE"):
            if m in rule.methods and rule.rule not in ("/login", "/auth/login", "/setup"):
                yield m, rule.rule.replace("<int:uid>", "1")


@pytest.mark.parametrize("body", [[], ["x"], "str", 5, None])
def test_no_mutating_route_500s_on_wrong_json_type(auth_client, body, monkeypatch):
    # A `null` body is a valid empty request — never let it start a REAL
    # 730-day backfill thread that outlives this test.
    monkeypatch.setattr(sync, "start_backfill_async", lambda **k: False)
    application = auth_client.application
    for method, path in _mutating_rules(application):
        r = auth_client.open(path, method=method, data=json.dumps(body),
                             content_type="application/json")
        # 502 is a deliberate "upstream failed" envelope (e.g. the Sunsynk test
        # button with nothing configured) — a crash would be 500.
        assert r.status_code != 500, (method, path, body)
        assert r.is_json and "success" in r.get_json(), (method, path, body)


def test_totp_unicode_digits_rejected_not_500(client):
    uid = db.insert_user("t", auth.hash_password("supersecret1"))
    auth.store_totp_secret(uid, totp.generate_secret())
    r = client.post("/login", data={"username": "t", "password": "supersecret1",
                                    "totp": "١٢٣٤٥٦"})
    assert r.status_code == 401


@pytest.mark.parametrize("bad", ["nan", "inf", "-inf", "1e999", "abc", -5, True])
def test_system_numbers_must_be_finite_and_non_negative(auth_client, bad):
    r = auth_client.post("/api/settings/system", json={"system_cost_gbp": bad})
    assert r.status_code == 400
    assert config.get_number("system_cost_gbp") is None


def test_system_number_valid_and_roi_strict_json(auth_client):
    r = auth_client.post("/api/settings/system", json={"system_cost_gbp": "12000"})
    assert r.status_code == 200
    assert config.get_number("system_cost_gbp") == 12000.0
    db.upsert_daily_summary({"date": "2026-09-01", "house_load_kwh": 10,
                             "grid_import_kwh": 2, "export_earned_pence": 50})
    body = _strict_json(auth_client.get("/api/roi"))
    assert body["data"]["configured"] is True


def test_legacy_stored_nan_is_ignored(auth_client):
    db.setting_set("cfg:system_cost_gbp", "nan")             # pre-fix data
    assert config.get_number("system_cost_gbp") is None
    body = _strict_json(auth_client.get("/api/roi"))
    assert body["data"] == {"configured": False}


@pytest.mark.parametrize("field,val", [("weather_lat", "95"), ("weather_lon", "-181"),
                                      ("weather_lat", "nan")])
def test_weather_location_range_checked(auth_client, field, val):
    assert auth_client.post("/api/settings/system",
                            json={field: val}).status_code == 400


@pytest.mark.parametrize("url", ["file:///etc/passwd", "gopher://x/1",
                                 "mattermost.local/hooks/x", "javascript:alert(1)"])
def test_webhook_url_must_be_http(auth_client, url):
    r = auth_client.post("/api/settings/credentials",
                         json={"MATTERMOST_WEBHOOK_URL": url})
    assert r.status_code == 400
    r = auth_client.post("/api/settings/test/webhook",
                         json={"MATTERMOST_WEBHOOK_URL": url})
    assert r.status_code == 400


def test_test_endpoints_reject_non_string_credentials(auth_client):
    r = auth_client.post("/api/settings/test/sunsynk",
                         json={"SUNSYNK_USERNAME": 5, "SUNSYNK_PASSWORD": ["x"]})
    assert r.status_code == 400


def test_sunsynk_test_closes_client_on_failure(auth_client, monkeypatch):
    closed = []
    monkeypatch.setattr(sunsynk.SunsynkClient, "close",
                        lambda self: closed.append(True))
    r = auth_client.post("/api/settings/test/sunsynk",
                         json={"SUNSYNK_USERNAME": "u", "SUNSYNK_PASSWORD": "p",
                               "SUNSYNK_PLANT_ID": "1"})
    assert r.status_code == 502 and closed


# ---------- TOTP secrets encrypted at rest ---------------------------------

def test_totp_secret_is_encrypted_and_login_works(auth_client):
    secret = auth_client.post("/api/2fa/setup").get_json()["data"]["secret"]
    code = totp._hotp(secret, int(time.time() // 30))
    r = auth_client.post("/api/2fa/enable", json={"code": code})
    assert r.get_json()["success"]
    stored = db.get_user_by_username("admin")["totp_secret"]
    assert stored.startswith("gAAAA") and secret not in stored
    assert auth.totp_secret_of(db.get_user_by_username("admin")) == secret


def test_legacy_plaintext_totp_is_migrated():
    uid = db.insert_user("legacy", auth.hash_password("supersecret1"))
    secret = totp.generate_secret()
    db.set_user_totp_raw(uid, secret)                       # pre-fix plaintext
    assert auth.totp_secret_of(db.get_user(uid)) == secret  # still verifies
    assert auth.migrate_totp_secrets() == 1
    row = db.get_user(uid)
    assert row["totp_secret"].startswith("gAAAA")
    assert auth.totp_secret_of(row) == secret
    assert auth.migrate_totp_secrets() == 0                 # idempotent


# ---------- new-IP notification off the request path ----------------------

def test_new_ip_notification_does_not_block_login(client, monkeypatch):
    uid = db.insert_user("u", auth.hash_password("supersecret1"))
    db.audit_add(uid, "u", "user.login", ip_address="10.9.9.9")   # a prior IP
    sent = threading.Event()

    def slow_post(text, **k):
        time.sleep(1.5)
        sent.set()
        return True, "ok"
    import notifications
    monkeypatch.setattr(notifications, "post_brief", slow_post)
    t0 = time.monotonic()
    r = client.post("/login", data={"username": "u", "password": "supersecret1"})
    assert r.status_code == 302
    assert time.monotonic() - t0 < 1.0
    assert sent.wait(5)


# ---------- P2-4: Octopus call budget + discovery cache ---------------------

def _fake_octopus(monkeypatch):
    for k, v in {"OCTOPUS_API_KEY": "sk_test", "OCTOPUS_ACCOUNT_NUMBER": "A-TEST",
                 "OCTOPUS_MPAN": "111", "OCTOPUS_METER_SERIAL": "LIVE",
                 "OCTOPUS_EXPORT_MPAN": "222", "OCTOPUS_EXPORT_SERIAL": "EXP1",
                 "OCTOPUS_MPRN": "333", "OCTOPUS_GAS_SERIAL": "GASA"}.items():
        config.set_credential(k, v)
    account = {"properties": [{"electricity_meter_points": [
        {"mpan": "111", "is_export": False,
         "meters": [{"serial_number": "DEAD"}, {"serial_number": "LIVE"}],
         "agreements": [{"tariff_code": "E-1R-COSY-22-12-08-P",
                         "valid_from": "2024-01-01T00:00:00Z", "valid_to": None}]},
        {"mpan": "222", "is_export": True, "meters": [{"serial_number": "EXP1"}],
         "agreements": [{"tariff_code": "E-1R-OUTGOING-FIX-12M-19-05-13-P",
                         "valid_from": "2024-01-01T00:00:00Z", "valid_to": None}]}],
        "gas_meter_points": [{"mprn": "333",
                              "meters": [{"serial_number": "GASA"}, {"serial_number": "GASB"}],
                              "agreements": [{"tariff_code": "G-1R-VAR-22-11-01-P",
                                              "valid_from": "2024-01-01T00:00:00Z",
                                              "valid_to": None}]}]}]}
    calls = collections.Counter()

    def fake_get(self, path, params=None):
        kind = ("account" if "/accounts/" in path else "consumption" if "consumption" in path
                else "unit-rates" if "unit-rates" in path
                else "standing" if "standing" in path else "products")
        calls[kind] += 1
        if kind == "account":
            return account
        if kind == "consumption":
            live = "/LIVE/" in path or "/EXP1/" in path
            return {"count": 1 if live else 0, "next": None, "results": (
                [{"interval_start": "2026-09-29T00:00:00Z",
                  "interval_end": "2026-09-29T00:30:00Z", "consumption": 0.1}]
                if live else [])}
        if kind == "products":
            return {"next": None, "results": [{"display_name": "Flexible Octopus",
                                               "direction": "IMPORT",
                                               "code": "VAR-22-11-01"}]}
        return {"next": None, "results": [{"value_inc_vat": 20.0,
                                           "valid_from": "2024-01-01T00:00:00Z",
                                           "valid_to": None}]}
    monkeypatch.setattr(octopus.OctopusClient, "_get", fake_get)
    return calls


def test_run_daily_octopus_call_budget(monkeypatch):
    calls = _fake_octopus(monkeypatch)
    sync.run_daily("2026-09-29")
    first = sum(calls.values())
    assert first <= 15, dict(calls)            # was 21 (discovery probes ×3 dates)
    calls.clear()
    sync.run_daily("2026-09-29")
    assert calls["account"] == 0 and calls["products"] == 0, dict(calls)
    assert sum(calls.values()) <= 8, dict(calls)   # only data-carrying requests


def test_meter_cache_cleared_when_octopus_credentials_change(auth_client, monkeypatch):
    _fake_octopus(monkeypatch)
    sync.run_daily("2026-09-29")
    assert db.setting_get(sync._METER_CACHE_KEY)
    auth_client.post("/api/settings/credentials",
                     json={"OCTOPUS_ACCOUNT_NUMBER": "A-OTHER"})
    assert not db.setting_get(sync._METER_CACHE_KEY)


def test_pagination_does_not_follow_off_origin_links(monkeypatch):
    c = octopus.OctopusClient("k", "A-1")
    seen = []

    def fake_get(path, params=None):
        seen.append(path)
        return {"results": [{"a": 1}], "next": "https://evil.example/steal?page=2"}
    monkeypatch.setattr(c, "_get", fake_get)
    assert c._get_paginated("/products/") == [{"a": 1}]
    assert seen == ["/products/"]


# ---------- live tariff rates (stored periods) ------------------------------

def test_refresh_tariff_rates_stores_all_periods_and_rate_at_resolves(monkeypatch):
    config.set_credential("OCTOPUS_API_KEY", "k")
    config.set_credential("OCTOPUS_ACCOUNT_NUMBER", "A-1")
    monkeypatch.setattr(sync, "resolve_meters", lambda s, o=None, **k: octopus.MeterInfo(
        import_tariff_code="E-1R-COSY-22-12-08-P",
        export_tariff_code="E-1R-OUTGOING-FIX-12M-19-05-13-P"))
    day0 = datetime.combine(sync.today_date(), datetime.min.time(), LONDON)
    periods = []
    for h0, h1, v in ((0, 4, 25.0), (4, 7, 13.0), (7, 13, 25.0), (13, 16, 13.0),
                      (16, 19, 38.0), (19, 22, 25.0), (22, 24, 13.0)):
        periods.append({"valid_from": (day0 + timedelta(hours=h0)).astimezone(timezone.utc)
                        .strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "valid_to": (day0 + timedelta(hours=h1)).astimezone(timezone.utc)
                        .strftime("%Y-%m-%dT%H:%M:%SZ"), "value_inc_vat": v})
    monkeypatch.setattr(octopus.OctopusClient, "get_unit_rates",
                        lambda self, p, t, f, a, b: periods if "COSY" in t
                        else [{"valid_from": "2024-01-01T00:00:00Z",
                               "valid_to": None, "value_inc_vat": 15.0}])
    monkeypatch.setattr(octopus.OctopusClient, "get_standing_charge",
                        lambda self, *a, **k: 50.0)
    assert sync.refresh_tariff_rates() == 8
    for h in (1, 5, 10, 14, 17, 20, 23):     # every window resolves now
        assert db.rate_at("electricity_import",
                          (day0 + timedelta(hours=h, minutes=10)).isoformat())
    assert db.rate_at("electricity_export", datetime.now(LONDON).isoformat())


def test_live_rate_and_window_until_from_stored_period(auth_client, monkeypatch):
    now = datetime.now(LONDON)
    start = now - timedelta(minutes=20)
    end = now + timedelta(minutes=40)
    db.upsert_tariff_rate("electricity_import",
                          start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                          21.5, end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    config.set_text("import_tariff_code", "E-1R-AGILE-24-10-01-P")
    t = auth_client.get("/api/live").get_json()["data"]["tariff"]
    assert t["import_rate_p"] == 21.5
    assert datetime.fromisoformat(t["window_until"]) == end.replace(second=0, microsecond=0) \
        or abs((datetime.fromisoformat(t["window_until"]) - end).total_seconds()) < 1


# ---------- GraphQL cold-cache failure TTL ----------------------------------

def test_graphql_cold_cache_failure_is_negatively_cached(monkeypatch):
    import octopus_graphql as gql
    config.set_credential("OCTOPUS_API_KEY", "k")
    config.set_credential("OCTOPUS_ACCOUNT_NUMBER", "A-1")
    posts = []

    def failing_post(payload, headers=None):
        posts.append(1)
        raise gql.GraphQLError("HTTP 503")
    monkeypatch.setattr(gql, "_post", failing_post)
    assert gql.get_account_overview()["available"] is False
    assert gql.get_account_overview()["available"] is False
    assert len(posts) == 1          # second call served from the failure TTL


def test_graphql_failure_drops_cached_token(monkeypatch):
    import octopus_graphql as gql
    config.set_credential("OCTOPUS_API_KEY", "k")
    config.set_credential("OCTOPUS_ACCOUNT_NUMBER", "A-1")
    gql._token_cache.update(ts=time.time(), token="stale", key="k")
    monkeypatch.setattr(gql, "_post", lambda *a, **k: (_ for _ in ()).throw(
        gql.GraphQLError("401 token expired")))
    gql.get_latest_statements(force=True)
    assert gql._token_cache["token"] is None


# ---------- carbon: contiguous 3h window -------------------------------------

def test_carbon_greenest_window_requires_contiguous_slots():
    import carbon

    def slot(h, m, g):
        t0 = datetime(2026, 10, 1, h, m, tzinfo=timezone.utc)
        t1 = t0 + timedelta(minutes=30)
        return {"from": t0.strftime("%Y-%m-%dT%H:%MZ"), "to": t1.strftime("%Y-%m-%dT%H:%MZ"),
                "intensity": {"forecast": g}}
    # 00:00-03:00 contiguous at 200 g; then a GAP; the low run spans the gap.
    slots = [slot(i // 2, 30 * (i % 2), 200) for i in range(6)]
    gap = [slot(4, 0, 10), slot(4, 30, 10), slot(5, 0, 10),       # 03:00-04:00 missing
           slot(6, 0, 10), slot(6, 30, 10), slot(7, 0, 10)]       # 05:30-06:00 missing
    w = carbon._greenest_window_3h(slots + gap)
    assert w["from"] == slots[0]["from"] and w["avg_g"] == 200


# ---------- Sunsynk: 401 storm + DST autumn day -----------------------------

def test_concurrent_401s_relogin_once(monkeypatch):
    c = sunsynk.SunsynkClient(username="u", password="p", plant_id="1",
                              inverter_sn="SN", token_cache_path="/nonexistent/x.json")
    c._token, c._token_expiry = "old", time.time() + 3600
    logins = []

    def fake_relogin():
        time.sleep(0.05)
        logins.append(1)
        c._token, c._token_expiry = "new", time.time() + 3600
    monkeypatch.setattr(c, "_relogin_locked", fake_relogin)
    monkeypatch.setattr(c, "_load_cached_token", lambda: False)
    monkeypatch.setattr(c, "_clear_token", lambda if_token=None: None)

    def fake_get(url, params=None, headers=None, timeout=None):
        ok = headers["Authorization"] == "Bearer new"
        return SimpleNamespace(status_code=200 if ok else 401, text="",
                               headers={}, json=lambda: {"data": {}})
    monkeypatch.setattr(c.session, "get", fake_get)
    out = c._get_parallel("a", "b", "c")
    assert len(out) == 3 and len(logins) == 1


def test_autumn_dst_day_keeps_the_repeated_hour():
    base = datetime(2000, 1, 1)
    seq = ([base + timedelta(minutes=m) for m in range(0, 120, 5)]
           + [base + timedelta(minutes=m) for m in range(60, 1440, 5)])
    recs = [{"time": t.strftime("%H:%M"), "value": 1.0} for t in seq]
    assert sunsynk.integrate_series_kwh(recs, "kW", "2026-10-25") > 24.5   # 25h day
    assert sunsynk.integrate_series_kwh(recs, "kW", "2026-10-24") < 24.5   # unchanged


# ---------- caching headers / service worker --------------------------------

def test_static_urls_versioned_and_immutable(auth_client):
    html = auth_client.get("/").get_data(as_text=True)
    assert f"main.js?v={config.APP_VERSION}" in html
    with auth_client.get(f"/static/js/theme.js?v={config.APP_VERSION}") as r:
        assert "max-age=31536000" in r.headers.get("Cache-Control", "")
    with auth_client.get("/static/js/theme.js") as r:
        assert "max-age=31536000" not in r.headers.get("Cache-Control", "")


def test_public_routes_do_not_vary_on_cookie(client):
    for path in ("/api/health", "/static/js/theme.js", "/widget/today", "/sw.js"):
        with client.get(path) as r:
            assert "Cookie" not in r.headers.get("Vary", ""), path
            assert "Set-Cookie" not in r.headers, path


def test_service_worker_denylists_financial_endpoints(client):
    js = client.get("/sw.js").get_data(as_text=True)
    for p in ("/api/account", "/api/reconcile", "/api/export.csv"):
        assert f'"{p}"' in js
    assert config.APP_VERSION in js and "status === 401" in js


# ---------- health / metrics / heartbeat ------------------------------------

def test_health_and_metrics_report_backups(client, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BACKUP_DIR", tmp_path / "b")
    assert client.get("/api/health").get_json()["data"]["backup_ok"] is False
    db.backup_database()
    assert client.get("/api/health").get_json()["data"]["backup_ok"] is True
    tok = auth.home_api_generate_token()
    m = client.get("/metrics", headers={"X-PowerSOC-Token": tok}).get_data(as_text=True)
    assert "powersoc_last_backup_age_seconds" in m
    assert "powersoc_backup_dir_isolated 1" in m


def test_heartbeat_pings_success_and_fail_and_never_raises(monkeypatch):
    seen = []
    monkeypatch.setattr(config, "HEARTBEAT_URL", "https://hc.example/ping/abc")
    monkeypatch.setattr(requests, "get", lambda url, **k: seen.append(url))
    sync.heartbeat(True)
    sync.heartbeat(False)
    assert seen == ["https://hc.example/ping/abc", "https://hc.example/ping/abc/fail"]
    monkeypatch.setattr(requests, "get", lambda *a, **k: (_ for _ in ()).throw(
        requests.ConnectionError("down")))
    sync.heartbeat(True)                       # must not raise


def test_heartbeat_off_by_default(monkeypatch):
    called = []
    monkeypatch.setattr(requests, "get", lambda *a, **k: called.append(1))
    sync.heartbeat(True)
    assert not called


# ---------- DB indexes -------------------------------------------------------

def test_hot_queries_use_indexes():
    with db.conn() as c:
        plan = " ".join(r[3] for r in c.execute(
            "EXPLAIN QUERY PLAN SELECT rate_p_per_kwh FROM electricity_intervals "
            "WHERE tariff_window=? AND rate_p_per_kwh IS NOT NULL "
            "ORDER BY interval_start DESC LIMIT 1", ("peak",)))
        assert "idx_elec_window" in plan and "TEMP B-TREE" not in plan
        plan = " ".join(r[3] for r in c.execute(
            "EXPLAIN QUERY PLAN SELECT 1 FROM audit_log WHERE action='user.login' "
            "AND user_id=? AND ip_address=? LIMIT 1", (1, "x")))
        assert "idx_audit_action" in plan


# ---------- sync orchestration coverage --------------------------------------

def test_sync_octopus_day_degrades_per_source():
    class Octo:
        def day_bounds(self, d):
            return "a", "b"

        def get_unit_rates(self, *a):
            return []

        def fetch_electricity_import(self, *a, **k):
            raise octopus.OctopusError("HTTP 502")

        def fetch_electricity_export(self, mpan, serial, d, rates):
            return octopus.ConsumptionDay(d, [{
                "interval_start": f"{d}T00:00:00+0100", "interval_end": "x",
                "export_kwh": 1.0, "rate_p_per_kwh": 15.0, "earned_pence": 15.0}], 1, True)
    meters = octopus.MeterInfo(import_mpan="1", import_serial="I",
                               export_mpan="2", export_serial="E")
    counts = sync.sync_octopus_day(Octo(), meters, "2026-09-29")
    assert counts == {"octopus_elec": 0, "octopus_export": 1}
    log = {r["source"]: r["status"] for r in db.sync_log_recent()}
    assert log == {"octopus_elec": "error", "octopus_export": "partial"}


def _backfill_settings(monkeypatch):
    monkeypatch.setattr(sync.config.Settings, "load", staticmethod(lambda: SimpleNamespace(
        octopus_configured=True, sunsynk_configured=True, octopus_api_key="k",
        octopus_account_number="A-1", gas_units="m3", sunsynk_username="u",
        sunsynk_password="p", sunsynk_plant_id="1", sunsynk_inverter_sn="SN")))
    monkeypatch.setattr(sync, "resolve_meters", lambda s, o=None, **k: octopus.MeterInfo())
    monkeypatch.setattr(sync, "_standing_charges", lambda *a, **k: (None, None))
    monkeypatch.setattr(sync.time, "sleep", lambda s: None)


def test_backfill_stops_sunsynk_after_empty_streak_but_walks_octopus(monkeypatch):
    _backfill_settings(monkeypatch)
    monkeypatch.setattr(sync, "resolve_meters", lambda s, o=None, **k: octopus.MeterInfo(
        import_mpan="1", import_serial="I"))
    octo_days, ss_days = [], []
    monkeypatch.setattr(sync, "sync_octopus_day",
                        lambda o, m, d, gas_units="m3", only=None: octo_days.append(d) or {})
    monkeypatch.setattr(sunsynk.SunsynkClient, "fetch_day",
                        lambda self, d: ss_days.append(d) or sunsynk.DayResult(
                            date=d, empty=True))
    start = (sync.today_date() - timedelta(days=6)).isoformat()
    st = sync.run_backfill(start=start)
    assert len(octo_days) == 6                       # every day walked
    assert len(ss_days) == sync._SUNSYNK_EMPTY_STREAK  # then Sunsynk stops
    assert st["running"] is False and st["days_done"] == 6


def test_backfill_transient_sunsynk_failure_does_not_end_history(monkeypatch):
    _backfill_settings(monkeypatch)
    monkeypatch.setattr(sync, "sync_octopus_day", lambda *a, **k: {})
    ss_days = []
    monkeypatch.setattr(sunsynk.SunsynkClient, "fetch_day",
                        lambda self, d: ss_days.append(d) or sunsynk.DayResult(
                            date=d, ok=False))       # outage, not end-of-history
    start = (sync.today_date() - timedelta(days=6)).isoformat()
    sync.run_backfill(start=start)
    assert len(ss_days) == 6


def test_backfill_skips_settled_stored_days(monkeypatch):
    """A day whose sources are all complete (every half-hour, all priced)
    costs no API calls; since 2026-10-26 judged from the stored intervals,
    per source, not from the summary flags."""
    _backfill_settings(monkeypatch)
    monkeypatch.setattr(sync, "resolve_meters", lambda s, o=None, **k: octopus.MeterInfo(
        import_mpan="1", import_serial="I"))
    old = (sync.today_date() - timedelta(days=5)).isoformat()
    db.upsert_intervals("solar_intervals", [
        {"interval_start": f"{old}T{i // 2:02d}:{i % 2 * 30:02d}:00", "pv_kwh": 0.1,
         "grid_import_kwh": 0.0, "house_load_kwh": 0.2} for i in range(48)])
    db.upsert_intervals("electricity_intervals", [
        {"interval_start": f"{old}T{i // 2:02d}:{i % 2 * 30:02d}:00+0000",
         "interval_end": "x", "consumption_kwh": 0.1, "tariff_window": "day",
         "rate_p_per_kwh": 20.0, "cost_pence": 2.0}
        for i in range(sync.expected_intervals(old))])
    db.upsert_daily_summary(sync.compute_daily_summary(old))
    walked = []
    monkeypatch.setattr(sync, "sync_octopus_day",
                        lambda o, m, d, gas_units="m3", only=None: walked.append(d) or {})
    monkeypatch.setattr(sunsynk.SunsynkClient, "fetch_day",
                        lambda self, d: sunsynk.DayResult(date=d, empty=True))
    st = sync.run_backfill(start=(sync.today_date() - timedelta(days=6)).isoformat())
    assert old not in walked and st["skipped"] == 1


def test_sync_daily_cli_runs_housekeeping(tmp_path, monkeypatch):
    import sync_daily
    monkeypatch.setattr(config, "BACKUP_DIR", tmp_path / "b")
    # A configured source: the heartbeat now reports failure when none is
    # (see test_review_2026_10_21), and this test is the healthy path.
    config.set_credential("OCTOPUS_API_KEY", "sk_test")
    config.set_credential("OCTOPUS_ACCOUNT_NUMBER", "A-TEST")
    monkeypatch.setattr(sync, "refresh_tariff_rates", lambda: 0)
    beats = []
    monkeypatch.setattr(sync, "heartbeat", lambda ok=True: beats.append(ok))
    monkeypatch.setattr(sync, "run_daily_and_notify",
                        lambda d=None: {"date": "2026-09-29"})
    assert sync_daily.main(["sync_daily.py"]) == 0
    assert list((tmp_path / "b").glob("powersoc-*.db")) and beats == [True]
