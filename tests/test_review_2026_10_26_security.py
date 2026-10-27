"""Regression tests for the 2026-10-26 review: authentication and request
hardening. Written to fail first. Covers recovery codes at the login form,
2FA re-enrolment, TOTP/recovery-code races, the per-(IP, username) login
throttle, trusted-proxy client IPs, audit-log growth, request-size limits,
control characters in paths, atomic settings saves, and cookie flags.
Hermetic per the project rules (see conftest)."""
from __future__ import annotations

import json
import re
import time

import pytest

import app as app_module
import auth
import config
import database as db
import totp

LAN = {"REMOTE_ADDR": "10.0.0.50"}


@pytest.fixture
def client():
    application = app_module.create_app()
    application.config.update(TESTING=True)
    return application.test_client()


def _login(c, user, pw, code="", env=LAN, **kw):
    return c.post("/auth/login", data={"username": user, "password": pw, "totp": code},
                  environ_base=env, **kw)


def _totp_user(name="tina"):
    secret = totp.generate_secret()
    uid = db.insert_user(name, auth.hash_password("longenough1"), role="admin")
    auth.store_totp_secret(uid, secret)
    return uid, secret


def _now_code(secret, offset=0):
    return totp._hotp(secret, int(time.time() // 30) + offset)


# ---------- recovery codes ------------------------------------------------------

def test_login_form_accepts_a_recovery_code(client):
    db.insert_user("admin", auth.hash_password("supersecret1"))
    html = client.get("/auth/login").get_data(as_text=True)
    field = re.search(r'<input[^>]*name="totp"[^>]*>', html).group(0)
    # pattern="[0-9]*" blocked ~98% of (hex) recovery codes in the browser.
    assert "pattern=" not in field
    assert 'inputmode="numeric"' not in field
    assert "recovery code" in html


def test_new_recovery_codes_are_80_bit_and_work_once(client):
    uid, _ = _totp_user()
    codes = auth.new_recovery_codes(uid)
    assert len(codes) == 10
    assert all(re.fullmatch(r"[A-Z2-7]{4}(-[A-Z2-7]{4}){3}", c) for c in codes)
    r = _login(client, "tina", "longenough1", codes[0].lower())   # case/format-tolerant
    assert r.status_code == 302
    client.get("/auth/logout")
    assert _login(client, "tina", "longenough1", codes[0]).status_code == 401


def test_legacy_hex_recovery_codes_still_work(client):
    import hashlib
    uid, _ = _totp_user()
    db.set_recovery_codes(uid, json.dumps([hashlib.sha256(b"aabbccdd").hexdigest()]))
    assert _login(client, "tina", "longenough1", "AABB-CCDD").status_code == 302


def test_recovery_code_consumption_is_atomic(client):
    """Two concurrent logins holding the same row snapshot: one code, one use."""
    uid, _ = _totp_user()
    code = auth.new_recovery_codes(uid)[0]
    stale = db.get_user(uid)
    with client.application.test_request_context():
        assert auth._consume_recovery_code(stale, code) is True
        assert auth._consume_recovery_code(stale, code) is False


def test_totp_counter_claim_is_atomic():
    uid, _ = _totp_user()
    assert db.claim_totp_counter(uid, 100) is True
    assert db.claim_totp_counter(uid, 100) is False      # concurrent replay
    assert db.claim_totp_counter(uid, 99) is False
    assert db.claim_totp_counter(uid, 101) is True


# ---------- 2FA enrolment -------------------------------------------------------

def _session_for(client, uid, secret=None):
    with client.session_transaction() as s:
        s["user_id"] = uid
        s["epoch"] = 0


def test_replacing_active_2fa_needs_a_current_code(client):
    uid, secret = _totp_user()
    _session_for(client, uid)
    r = client.post("/api/2fa/setup", json={}, environ_base=LAN)
    assert r.status_code == 400 and "current" in r.get_json()["error"]
    r = client.post("/api/2fa/setup", json={"code": _now_code(secret)}, environ_base=LAN)
    assert r.status_code == 200 and r.get_json()["data"]["secret"]


def test_enrolment_code_cannot_be_replayed_at_login(client):
    uid = db.insert_user("nina", auth.hash_password("longenough1"), role="admin")
    _session_for(client, uid)
    secret = client.post("/api/2fa/setup", json={}, environ_base=LAN).get_json()["data"]["secret"]
    code = _now_code(secret)
    r = client.post("/api/2fa/enable", json={"code": code}, environ_base=LAN)
    assert r.status_code == 200 and len(r.get_json()["data"]["recovery_codes"]) == 10
    client.get("/auth/logout")
    assert _login(client, "nina", "longenough1", code).status_code == 401


def test_recovery_codes_can_be_regenerated_with_a_current_code(client):
    uid, secret = _totp_user()
    old = auth.new_recovery_codes(uid)
    _session_for(client, uid)
    assert client.post("/api/2fa/recovery-codes", json={},
                       environ_base=LAN).status_code == 400
    r = client.post("/api/2fa/recovery-codes", json={"code": _now_code(secret)},
                    environ_base=LAN)
    new = r.get_json()["data"]["recovery_codes"]
    assert len(new) == 10 and not set(new) & set(old)
    client.get("/auth/logout")
    assert _login(client, "tina", "longenough1", old[1]).status_code == 401


# ---------- login throttle ------------------------------------------------------

def test_another_accounts_success_does_not_reset_the_throttle(client, monkeypatch):
    """An attacker with their own account on the same IP (or behind the same
    proxy) used to reset the counter by logging in successfully."""
    monkeypatch.setattr(config, "LOGIN_MAX_FAILURES", 3)
    db.insert_user("admin", auth.hash_password("supersecret1"))
    db.insert_user("mallory", auth.hash_password("supersecret2"))
    for _ in range(3):
        _login(client, "admin", "guess")
    assert _login(client, "mallory", "supersecret2").status_code == 302
    client.get("/auth/logout")
    assert _login(client, "admin", "supersecret1").status_code == 429


def test_throttle_is_per_username_on_a_shared_ip(client, monkeypatch):
    monkeypatch.setattr(config, "LOGIN_MAX_FAILURES", 3)
    db.insert_user("admin", auth.hash_password("supersecret1"))
    db.insert_user("viewer", auth.hash_password("supersecret2"))
    for _ in range(3):
        _login(client, "admin", "guess")
    assert _login(client, "viewer", "supersecret2").status_code == 302


def test_throttle_has_a_per_ip_ceiling_across_usernames(client, monkeypatch):
    monkeypatch.setattr(config, "LOGIN_MAX_FAILURES", 3)
    monkeypatch.setattr(config, "LOGIN_MAX_FAILURES_PER_IP", 6)
    db.insert_user("admin", auth.hash_password("supersecret1"))
    for i in range(6):
        _login(client, f"spray{i}", "guess")
    assert _login(client, "admin", "supersecret1").status_code == 429


def test_throttled_attempts_do_not_grow_the_audit_log(client, monkeypatch):
    monkeypatch.setattr(config, "LOGIN_MAX_FAILURES", 2)
    db.insert_user("admin", auth.hash_password("supersecret1"))
    for _ in range(2):
        _login(client, "admin", "guess")
    for _ in range(40):
        _login(client, "admin" + "x" * 5000, "guess")
    rows = db.audit_list(limit=1000, action="user.login_failed")
    assert len(rows) <= 6
    assert all(len(r["username"] or "") <= 64 for r in rows)


# ---------- trusted proxy -------------------------------------------------------

def test_client_ip_comes_from_xff_only_via_a_trusted_proxy(monkeypatch):
    monkeypatch.setattr(config, "TRUSTED_PROXIES", config.parse_proxies("192.168.1.2"))
    application = app_module.create_app()
    application.config.update(TESTING=True)
    c = application.test_client()
    db.insert_user("admin", auth.hash_password("supersecret1"))
    _login(c, "admin", "guess", env={"REMOTE_ADDR": "192.168.1.2"},
           headers={"X-Forwarded-For": "203.0.113.9", "X-Forwarded-Proto": "https"})
    _login(c, "admin", "guess", env={"REMOTE_ADDR": "10.0.0.66"},
           headers={"X-Forwarded-For": "198.51.100.1"})
    ips = {r["ip_address"] for r in db.audit_list(action="user.login_failed")}
    assert ips == {"203.0.113.9", "10.0.0.66"}


def test_session_cookie_is_secure_behind_an_https_proxy(monkeypatch):
    monkeypatch.setattr(config, "TRUSTED_PROXIES", config.parse_proxies("192.168.1.2/32"))
    application = app_module.create_app()
    application.config.update(TESTING=True)
    c = application.test_client()
    db.insert_user("admin", auth.hash_password("supersecret1"))
    r = _login(c, "admin", "supersecret1", env={"REMOTE_ADDR": "192.168.1.2"},
               headers={"X-Forwarded-For": "203.0.113.9", "X-Forwarded-Proto": "https"})
    cookie = r.headers["Set-Cookie"]
    assert cookie.startswith("powersoc_session=")   # not the shared name "session"
    assert "Secure" in cookie
    lan = _login(c, "admin", "supersecret1")          # plain-HTTP LAN: no Secure
    assert "Secure" not in lan.headers["Set-Cookie"]


# ---------- request hardening ------------------------------------------------------

def test_oversized_bodies_are_refused_before_parsing(client):
    r = client.post("/setup", data=b"x" * (2 * 1024 * 1024),
                    content_type="application/json", environ_base=LAN)
    assert r.status_code == 413


def test_control_characters_in_the_path_are_not_a_500(client):
    db.insert_user("admin", auth.hash_password("supersecret1"))
    r = client.get("/x%0Ay", environ_base=LAN)
    assert r.status_code == 302
    assert "\n" not in r.headers["Location"] and "%0A" in r.headers["Location"]
    assert auth.safe_next_path("/a\nb") == "/"
    assert auth.safe_next_path("/a\tb") == "/"


def test_sensitive_responses_are_not_cached(client):
    uid = db.insert_user("admin", auth.hash_password("supersecret1"), role="admin")
    _session_for(client, uid)
    for path in ("/api/settings", "/api/users", "/api/settings/env-export", "/settings"):
        r = client.get(path, environ_base=LAN)
        assert r.status_code == 200, path
        assert r.headers.get("Cache-Control") == "no-store", path


def test_settings_credentials_save_is_all_or_nothing(client):
    uid = db.insert_user("admin", auth.hash_password("supersecret1"), role="admin")
    _session_for(client, uid)
    # Keys sort alphabetically on the wire: the valid one is applied first.
    r = client.post("/api/settings/credentials", environ_base=LAN,
                    json={"OCTOPUS_ACCOUNT_NUMBER": "A-NEW", "SUNSYNK_PLANT_ID": 42})
    assert r.status_code == 400
    assert config.get_credential("OCTOPUS_ACCOUNT_NUMBER") == ""


def test_settings_system_save_is_all_or_nothing(client):
    uid = db.insert_user("admin", auth.hash_password("supersecret1"), role="admin")
    _session_for(client, uid)
    r = client.post("/api/settings/system", environ_base=LAN,
                    json={"system_cost_gbp": 5000, "battery_nameplate_kwh": "abc"})
    assert r.status_code == 400
    assert config.get_number("system_cost_gbp") is None


def test_weather_cache_is_dropped_when_the_location_changes(client):
    import weather
    uid = db.insert_user("admin", auth.hash_password("supersecret1"), role="admin")
    _session_for(client, uid)
    weather._cache.update(ts=time.time(), data={"available": True, "location": "Old"})
    weather._fc_cache.update(ts=time.time(), data={"available": True})
    client.post("/api/settings/system", environ_base=LAN,
                json={"weather_lat": "55.0", "weather_lon": "-3.6"})
    assert weather._cache["data"] is None and weather._fc_cache["data"] is None
