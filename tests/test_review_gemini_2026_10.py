"""Regression tests for the 2026-10-07 fixes from the Gemini code review.

CSRF/cross-origin write guard (FINDING-01), audit-log-backed login throttle
(FINDING-03), key-seed location warning (FINDING-04), SQLite busy timeout
(FINDING-06). Hermetic per the project rules (see conftest).
"""
from __future__ import annotations

import logging

import pytest

import app as app_module
import auth
import config
import database as db
import sync

BASE = "http://192.168.1.50:8073"


@pytest.fixture
def client():
    application = app_module.create_app()
    application.config.update(TESTING=True)
    return application.test_client()


@pytest.fixture
def admin(client):
    r = client.post("/setup", base_url=BASE,
                    data={"setup_token": auth.setup_token(),
                          "username": "admin", "password": "supersecret1"})
    assert r.status_code in (302, 303)
    return client


@pytest.fixture
def backfill_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(sync, "start_backfill_async",
                        lambda **k: calls.append(k) or True)
    return calls


# ---------- FINDING-01: cross-origin (CSRF) write guard ---------------------

@pytest.mark.parametrize("headers,allowed", [
    ({"Sec-Fetch-Site": "same-origin"}, True),
    ({"Sec-Fetch-Site": "none"}, True),            # user-initiated
    ({}, True),                                    # non-browser client
    ({"Origin": BASE}, True),
    ({"Sec-Fetch-Site": "same-site"}, False),      # other port, same host
    ({"Sec-Fetch-Site": "cross-site"}, False),
    ({"Origin": "http://192.168.1.50:8090"}, False),  # another app's port, no SFS
    ({"Origin": "http://evil.example"}, False),
    ({"Origin": "null"}, False),                   # sandboxed iframe etc.
    ({"Referer": "http://evil.example/page"}, False),
])
def test_cross_origin_writes_blocked(admin, backfill_calls, headers, allowed):
    r = admin.post("/api/backfill/start", base_url=BASE, json={}, headers=headers)
    if allowed:
        assert r.status_code == 200 and len(backfill_calls) == 1
    else:
        assert r.status_code == 403 and backfill_calls == []
        assert r.get_json()["error"] == "cross-origin request blocked"


def test_reproduced_same_site_attack_is_blocked(admin):
    """The exact request that worked on 2026-10-07: a text/plain POST (no
    CORS preflight) from another app on the same host, admin cookie attached,
    disabling a user."""
    uid = db.insert_user("bob", auth.hash_password("supersecret1"))
    r = admin.post(f"/api/users/{uid}/disable", base_url=BASE, data="x=1",
                   content_type="text/plain",
                   headers={"Sec-Fetch-Site": "same-site",
                            "Origin": "http://192.168.1.50:8090"})
    assert r.status_code == 403
    assert not db.get_user(uid)["disabled"]


def test_same_origin_ui_writes_still_work(admin):
    r = admin.post("/api/settings/system", base_url=BASE,
                   json={"weather_name": "Home"},
                   headers={"Sec-Fetch-Site": "same-origin", "Origin": BASE})
    assert r.status_code == 200


def test_login_csrf_blocked_but_same_origin_login_works(client):
    db.insert_user("u", auth.hash_password("supersecret1"))
    r = client.post("/login", base_url=BASE,
                    data={"username": "u", "password": "supersecret1"},
                    headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403
    r = client.post("/login", base_url=BASE,
                    data={"username": "u", "password": "supersecret1"},
                    headers={"Sec-Fetch-Site": "same-origin"})
    assert r.status_code == 302


def test_login_form_over_plain_http_is_not_blocked(client):
    """2026-10-14: every login on http://192.168.1.50:8073 was 403'd. Over plain
    HTTP browsers send no Sec-Fetch-Site, and under Referrer-Policy:
    no-referrer a same-origin <form> POST carries Origin: null and no Referer
    (Fetch spec; seen live from Firefox 157, reproduced in Chromium). The
    tests above set Sec-Fetch-Site by hand, which only HTTPS/localhost get."""
    db.insert_user("u", auth.hash_password("supersecret1"))
    policy = client.get("/login", base_url=BASE).headers["Referrer-Policy"]
    # same-origin: Origin kept for our own posts, nothing sent cross-origin.
    assert policy == "same-origin"
    # What the browser sends for the login form under that policy.
    origin = "null" if policy == "no-referrer" else BASE
    r = client.post("/login", base_url=BASE,
                    data={"username": "u", "password": "supersecret1"},
                    headers={"Origin": origin, "Referer": f"{BASE}/login"})
    assert r.status_code == 302


def test_cross_site_reads_and_navigation_unaffected(admin):
    assert admin.get("/", base_url=BASE,
                     headers={"Sec-Fetch-Site": "cross-site"}).status_code == 200
    assert admin.get("/widget/today", base_url=BASE,
                     headers={"Sec-Fetch-Site": "cross-site"}).status_code == 200


def test_default_port_origin_matches(client):
    db.insert_user("u", auth.hash_password("supersecret1"))
    r = client.post("/login", base_url="https://powersoc.example",
                    data={"username": "u", "password": "supersecret1"},
                    headers={"Origin": "https://powersoc.example"})
    assert r.status_code == 302


def test_csp_restricts_form_targets(client):
    csp = client.get("/login").headers["Content-Security-Policy"]
    assert "form-action 'self'" in csp


# ---------- FINDING-03: throttle persisted in audit_log ---------------------

def _fail(client, n, user="admin"):
    for _ in range(n):
        client.post("/login", data={"username": user, "password": "wrong"})


def test_throttle_survives_restart_and_is_shared_across_workers(monkeypatch):
    monkeypatch.setattr(config, "LOGIN_MAX_FAILURES", 3)
    db.insert_user("admin", auth.hash_password("supersecret1"))
    worker_a = app_module.create_app().test_client()
    _fail(worker_a, 3)
    # A brand-new app instance (restart, or a second gunicorn worker) sees
    # the same lockout — the state is in the DB, not process memory.
    worker_b = app_module.create_app().test_client()
    r = worker_b.post("/login", data={"username": "admin", "password": "supersecret1"})
    assert r.status_code == 429


def test_throttle_counts_since_last_success_only(client, monkeypatch):
    monkeypatch.setattr(config, "LOGIN_MAX_FAILURES", 3)
    db.insert_user("admin", auth.hash_password("supersecret1"))
    _fail(client, 2)
    assert client.post("/login", data={"username": "admin",
                                       "password": "supersecret1"}).status_code == 302
    _fail(client, 2)          # 2 since the success: still under the limit
    assert client.post("/login", data={"username": "admin",
                                       "password": "supersecret1"}).status_code == 302


def test_throttled_attempts_do_not_extend_the_lockout(client, monkeypatch):
    monkeypatch.setattr(config, "LOGIN_MAX_FAILURES", 3)
    t = {"now": 2_000_000_000.0}
    monkeypatch.setattr(auth.time, "time", lambda: t["now"])
    db.insert_user("admin", auth.hash_password("supersecret1"))
    _fail(client, 3)
    for _ in range(5):                      # hammering while locked out
        t["now"] += 30
        assert client.post("/login", data={"username": "admin",
                                           "password": "wrong"}).status_code == 429
    t["now"] = 2_000_000_000.0 + config.LOGIN_WINDOW_SECONDS + 1
    assert client.post("/login", data={"username": "admin",
                                       "password": "supersecret1"}).status_code == 302


def test_throttle_is_per_ip(monkeypatch):
    monkeypatch.setattr(config, "LOGIN_MAX_FAILURES", 2)
    db.insert_user("admin", auth.hash_password("supersecret1"))
    c = app_module.create_app().test_client()
    for _ in range(2):
        c.post("/login", data={"username": "admin", "password": "x"},
               environ_base={"REMOTE_ADDR": "10.0.0.66"})
    blocked = c.post("/login", data={"username": "admin", "password": "supersecret1"},
                     environ_base={"REMOTE_ADDR": "10.0.0.66"})
    other = c.post("/login", data={"username": "admin", "password": "supersecret1"},
                   environ_base={"REMOTE_ADDR": "10.0.0.77"})
    assert blocked.status_code == 429 and other.status_code == 302


def test_failure_rows_use_the_app_clock(client, monkeypatch):
    monkeypatch.setattr(auth.time, "time", lambda: 1_700_000_000.0)
    db.insert_user("admin", auth.hash_password("supersecret1"))
    _fail(client, 1)
    row = db.audit_list(action="user.login_failed")[0]
    assert row["created_at"] == "2023-11-14 22:13:20"


def test_no_in_memory_throttle_state_remains():
    assert not hasattr(auth, "_login_fails")


# ---------- FINDING-04: key-seed location ------------------------------------

def test_key_seed_source_order(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "_MACHINE_ID_PATHS", ())
    monkeypatch.delenv("POWERSOC_MACHINE_ID", raising=False)
    monkeypatch.delenv("POWERSOC_MACHINE_ID_FILE", raising=False)
    assert config.key_seed_source() == "hostname"
    (tmp_path / ".instance_id").write_text("abc")
    assert config.key_seed_source() == "instance-id"
    assert config.key_seed_is_external() is False
    monkeypatch.setenv("POWERSOC_MACHINE_ID_FILE", str(tmp_path / "x"))
    assert config.key_seed_source() == "file"
    monkeypatch.setenv("POWERSOC_MACHINE_ID", "s")
    assert config.key_seed_source() == "env" and config.key_seed_is_external()


def test_startup_warns_every_time_seed_is_in_data_dir(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(config, "key_seed_is_external", lambda: False)
    monkeypatch.setattr(config, "key_seed_source", lambda: "instance-id")
    with caplog.at_level(logging.WARNING, logger="powersoc.app"):
        app_module.create_app()
    assert any("key seed" in r.getMessage() for r in caplog.records)
    caplog.clear()
    monkeypatch.setattr(config, "key_seed_is_external", lambda: True)
    with caplog.at_level(logging.WARNING, logger="powersoc.app"):
        app_module.create_app()
    assert not any("key seed" in r.getMessage() for r in caplog.records)


def test_metrics_reports_key_seed_location(client):
    tok = auth.home_api_generate_token()
    m = client.get("/metrics", headers={"X-PowerSOC-Token": tok}).get_data(as_text=True)
    assert "powersoc_key_seed_external" in m


# ---------- FINDING-06: SQLite busy timeout ------------------------------------

def test_sqlite_busy_timeout_is_30s_and_configurable(monkeypatch):
    with db.conn() as c:
        assert c.execute("PRAGMA busy_timeout").fetchone()[0] == 30000
    monkeypatch.setattr(db, "DB_TIMEOUT_S", 5.0)
    with db.conn() as c:
        assert c.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
