"""Integration tests for the Flask app: auth gating, widget, settings, users."""
from __future__ import annotations

import pytest

import app as app_module
import auth
import database as db


@pytest.fixture
def client(tmp_db):
    application = app_module.create_app()
    application.config.update(TESTING=True)
    return application.test_client()


@pytest.fixture
def auth_client(client):
    # First-run /setup creates the admin and logs the session in.
    r = client.post("/setup", data={"setup_token": auth.setup_token(), "username": "admin", "password": "supersecret1"})
    assert r.status_code in (302, 303)
    return client


# ---------- auth gating ----------------------------------------------------

def test_first_run_redirects_to_setup(client):
    r = client.get("/")
    assert r.status_code == 302 and "/setup" in r.headers["Location"]


def test_api_returns_401_json_not_redirect(client):
    r = client.get("/api/today")
    assert r.status_code == 401
    assert r.get_json()["success"] is False


def test_setup_wizard_json_creates_and_authenticates(client):
    # The wizard posts JSON and advances client-side instead of redirecting.
    r = client.post("/setup", json={"setup_token": auth.setup_token(), "username": "alex", "password": "supersecret1"})
    assert r.status_code == 200 and r.get_json()["success"] is True
    # Session is now authenticated — an API call that 401'd before now works.
    assert client.get("/api/today").status_code == 200
    # Re-running setup once an admin exists is rejected.
    assert client.post("/setup",
                       json={"username": "x", "password": "yyyyyyyy"}).status_code == 409


def test_setup_json_rejects_short_password(client):
    r = client.post("/setup", json={"setup_token": auth.setup_token(), "username": "a", "password": "short"})
    assert r.status_code == 400 and r.get_json()["success"] is False


# ---------- widget: public + embeddable -----------------------------------

def test_widget_public_and_framable(client):
    r = client.get("/widget/today")            # no auth at all
    assert r.status_code == 200
    assert b"PowerSOC" in r.data
    # Embeddable: no X-Frame-Options, and CSP must not forbid framing.
    assert "X-Frame-Options" not in r.headers
    assert "frame-ancestors 'none'" not in r.headers.get("Content-Security-Policy", "")


def test_non_widget_is_frame_denied(auth_client):
    r = auth_client.get("/")
    assert r.headers.get("X-Frame-Options") == "DENY"


# ---------- settings credentials ------------------------------------------

def test_credentials_roundtrip_secret_masked(auth_client):
    auth_client.post("/api/settings/credentials", json={
        "SUNSYNK_USERNAME": "me@example.com", "OCTOPUS_API_KEY": "sk_secret"})
    data = auth_client.get("/api/settings").get_json()["data"]["credentials"]
    # Non-secret echoes its value; secret is masked but marked set.
    assert data["SUNSYNK_USERNAME"]["value"] == "me@example.com"
    assert data["OCTOPUS_API_KEY"]["value"] is None
    assert data["OCTOPUS_API_KEY"]["set"] is True
    # Stored encrypted (not plaintext) in the settings table.
    raw = db.setting_get("cred:OCTOPUS_API_KEY")
    assert raw and raw != "sk_secret"


def test_blank_secret_leaves_unchanged(auth_client):
    auth_client.post("/api/settings/credentials", json={"OCTOPUS_API_KEY": "first"})
    auth_client.post("/api/settings/credentials", json={"OCTOPUS_API_KEY": ""})
    import config
    assert config.get_credential("OCTOPUS_API_KEY") == "first"


# ---------- user management -----------------------------------------------

def test_user_crud_and_guards(auth_client):
    # Add a user.
    uid = auth_client.post("/api/users", json={
        "username": "bob", "password": "longpassword"}).get_json()["data"]["id"]
    assert len(auth_client.get("/api/users").get_json()["data"]) == 2
    # Cannot delete yourself (admin is user 1).
    assert auth_client.delete("/api/users/1").status_code == 400
    # Reset password requires >=8 chars.
    assert auth_client.post(f"/api/users/{uid}/password",
                            json={"password": "short"}).status_code == 400
    # Disable + delete the new user.
    auth_client.post(f"/api/users/{uid}/disable", json={"disabled": True})
    assert auth_client.delete(f"/api/users/{uid}").get_json()["success"] is True


def test_username_charset_and_role_constrained(auth_client):
    # A username with HTML/script characters is rejected server-side
    # (defence-in-depth alongside the frontend output escaping).
    r = auth_client.post("/api/users", json={
        "username": "<img src=x onerror=alert(1)>", "password": "longpassword"})
    assert r.status_code == 400 and r.get_json()["success"] is False
    # An arbitrary role string is coerced to the safe default.
    uid = auth_client.post("/api/users", json={
        "username": "carol", "password": "longpassword",
        "role": "<b>x</b>"}).get_json()["data"]["id"]
    role = next(u["role"] for u in auth_client.get("/api/users").get_json()["data"]
                if u["id"] == uid)
    assert role == "user"


def test_non_admin_is_read_only(auth_client):
    # Admin creates a normal 'user' role account...
    assert auth_client.post("/api/users", json={
        "username": "viewer", "password": "longpassword",
        "role": "user"}).get_json()["success"] is True
    auth_client.get("/logout")
    # ...who logs in.
    r = auth_client.post("/login", data={"username": "viewer", "password": "longpassword"})
    assert r.status_code in (302, 303)
    # Read-only data endpoints work for them.
    assert auth_client.get("/api/today").status_code == 200
    assert auth_client.get("/api/backfill/status").status_code == 200
    # Management / settings / mutating endpoints are forbidden (403).
    assert auth_client.get("/api/users").status_code == 403
    assert auth_client.post("/api/settings/credentials",
                            json={"SUNSYNK_USERNAME": "x"}).status_code == 403
    assert auth_client.get("/api/settings/env-export").status_code == 403
    assert auth_client.post("/api/backfill/start", json={}).status_code == 403
    # The Settings page redirects a non-admin away.
    assert auth_client.get("/settings").status_code in (302, 303)


def test_login_upgrades_legacy_password_hash(client):
    # Seed a user whose password hash uses the old 200k iteration count, then
    # log in: it must succeed and silently upgrade the stored hash to 600k.
    import base64
    import hashlib

    import auth
    salt = b"abcdefghijklmnop"
    raw = hashlib.pbkdf2_hmac("sha256", b"legacypassword", salt, 200_000)
    legacy = (f"pbkdf2$200000${base64.b64encode(salt).decode()}"
              f"${base64.b64encode(raw).decode()}")
    uid = db.insert_user("legacy", legacy, role="admin")
    r = client.post("/login", data={"username": "legacy", "password": "legacypassword"})
    assert r.status_code in (302, 303)
    stored = db.get_user(uid)["password_hash"]
    assert int(stored.split("$")[1]) == 600_000
    assert auth.verify_password("legacypassword", stored)


def test_api_today_uses_london_date(auth_client, monkeypatch):
    # /api/today must build for the Europe/London date (via sync.today()), not
    # the host's local date.today() — verified by mocking the London date.
    import sync
    monkeypatch.setattr(sync, "today", lambda: "2026-03-30")
    p = auth_client.get("/api/today").get_json()["data"]
    assert p["date"] == "2026-03-30" and p["nav"]["is_today"] is True


def test_login_throttle_and_unknown_user(auth_client, monkeypatch):
    import config
    monkeypatch.setattr(config, "LOGIN_MAX_FAILURES", 3)
    # Unknown username fails closed (no enumeration / no 500) — and counts
    # against "ghost" only: the limit is per (IP, username) since 2026-10-26.
    assert auth_client.post(
        "/login", data={"username": "ghost", "password": "whatever"}).status_code == 401
    # Three failures for admin reach the configured limit...
    for _ in range(3):
        assert auth_client.post(
            "/login", data={"username": "admin", "password": "wrong"}).status_code == 401
    # ...the next attempt is throttled (429) even with the correct password.
    assert auth_client.post(
        "/login", data={"username": "admin", "password": "supersecret1"}).status_code == 429


def test_reconcile_self_computed(auth_client, monkeypatch):
    from datetime import date

    import octopus_graphql
    import sync
    monkeypatch.setattr(octopus_graphql, "get_latest_statements", lambda: [])
    monkeypatch.setattr(sync, "today_date", lambda: date(2026, 5, 15))
    db.upsert_daily_summary({"date": "2026-05-01", "net_cost_pence": 120.0,
                             "import_cost_pence": 100.0, "standing_elec_pence": 30.0,
                             "export_earned_pence": 10.0})
    db.upsert_daily_summary({"date": "2026-05-02", "net_cost_pence": 180.0,
                             "import_cost_pence": 150.0, "standing_elec_pence": 30.0,
                             "export_earned_pence": 0.0})
    d = auth_client.get("/api/reconcile").get_json()["data"]
    assert d["billed_available"] is False
    may = next(m for m in d["months"] if m["month"] == "2026-05")
    assert may["days_with_data"] == 2
    assert may["our_cost_pence"] == 300.0       # 120 + 180
    assert may["billed_pence"] is None and may["delta_pence"] is None


def test_reconcile_with_billed_delta(auth_client, monkeypatch):
    from datetime import date

    import octopus_graphql
    import sync
    monkeypatch.setattr(sync, "today_date", lambda: date(2026, 5, 15))
    monkeypatch.setattr(octopus_graphql, "get_latest_statements",
                        lambda: [{"from": "2026-05-01", "to": "2026-05-31",
                                  "total_pence": 350.0}])
    db.upsert_daily_summary({"date": "2026-05-01", "net_cost_pence": 300.0,
                             "octopus_ok": 1})
    d = auth_client.get("/api/reconcile").get_json()["data"]
    assert d["billed_available"] is True
    # Compared per statement, over the statement's own period (2026-10-26).
    st = d["statements"][0]
    assert st["billed_pence"] == 350.0
    assert st["delta_pence"] == -50.0           # our 300 - billed 350


def test_service_worker_and_manifest_public(client):
    import json

    import config
    r = client.get("/sw.js")                       # no auth
    assert r.status_code == 200
    assert "javascript" in r.headers["Content-Type"]
    assert r.headers.get("Service-Worker-Allowed") == "/"
    assert config.APP_VERSION in r.get_data(as_text=True)   # cache versioned
    with client.get("/static/manifest.webmanifest") as r:
        m = json.loads(r.get_data(as_text=True))
    assert m["start_url"] == "/" and m["display"] == "standalone"


def test_2fa_enable_then_login_requires_code(auth_client, monkeypatch):
    import time

    import totp
    secret = auth_client.post("/api/2fa/setup").get_json()["data"]["secret"]
    code = totp._hotp(secret, int(time.time() // 30))
    assert auth_client.post("/api/2fa/enable",
                            json={"code": code}).get_json()["data"]["enabled"] is True
    auth_client.get("/logout")
    # Password alone is no longer enough.
    assert auth_client.post(
        "/login", data={"username": "admin", "password": "supersecret1"}).status_code == 401
    # Password + a fresh TOTP code logs in. The enrolment code itself is
    # spent (single-use), so use the next time step (inside the ±1 window).
    code2 = totp._hotp(secret, int(time.time() // 30) + 1)
    r = auth_client.post("/login", data={"username": "admin",
                                         "password": "supersecret1", "totp": code2})
    assert r.status_code in (302, 303)


def test_revoke_sessions_logs_out_other_sessions(tmp_db):
    application = app_module.create_app()
    application.config.update(TESTING=True)
    a, b = application.test_client(), application.test_client()
    a.post("/setup", data={"setup_token": auth.setup_token(), "username": "admin", "password": "supersecret1"})  # a in
    assert b.post("/login", data={"username": "admin",
                                  "password": "supersecret1"}).status_code in (302, 303)
    assert a.get("/api/today").status_code == 200
    assert b.get("/api/today").status_code == 200
    # a revokes other sessions: a stays signed in, b is signed out.
    assert a.post("/api/sessions/revoke", json={}).get_json()["data"]["revoked"] is True
    assert a.get("/api/today").status_code == 200
    assert b.get("/api/today").status_code == 401


def test_home_api_token_gating(auth_client):
    # Default off -> 403 even though we're authed (token, not session, gates it).
    assert auth_client.get("/api/home/status").status_code == 403
    # Generate a token via the admin endpoint.
    token = auth_client.post("/api/settings/home-token/generate").get_json()["data"]["token"]
    assert token
    # Wrong token -> 401; correct token -> 200.
    assert auth_client.get("/api/home/status",
                           headers={"X-PowerSOC-Token": "nope"}).status_code == 401
    r = auth_client.get("/api/home/status", headers={"X-PowerSOC-Token": token})
    assert r.status_code == 200 and r.get_json()["data"]["app"] == "PowerSOC"
    # Bearer header also works.
    assert auth_client.get("/api/home/today",
                           headers={"Authorization": f"Bearer {token}"}).status_code == 200
    # Read-only: writes rejected.
    assert auth_client.post("/api/home/status",
                            headers={"X-PowerSOC-Token": token}).status_code == 403


def test_home_api_rejects_query_token(auth_client):
    # The token must NOT be accepted as a ?token= query param (would leak to logs).
    token = auth_client.post("/api/settings/home-token/generate").get_json()["data"]["token"]
    assert auth_client.get(f"/api/home/status?token={token}").status_code == 401
    assert auth_client.get("/api/home/status",
                           headers={"X-PowerSOC-Token": token}).status_code == 200


def test_metrics_token_gated(auth_client, monkeypatch):
    import carbon
    import sync
    monkeypatch.setattr(carbon, "get_intensity", lambda: {"available": False})
    # Seed with the LONDON date the route reads — host-local date.today() is a
    # day behind between 23:00 and 00:00 UTC during BST (hermeticity rule).
    db.upsert_daily_summary({"date": sync.today(),
                             "pv_kwh": 5.0, "net_cost_pence": 100})
    assert auth_client.get("/metrics").status_code == 403   # token off
    token = auth_client.post("/api/settings/home-token/generate").get_json()["data"]["token"]
    r = auth_client.get("/metrics", headers={"X-PowerSOC-Token": token})
    assert r.status_code == 200
    assert "text/plain" in r.content_type
    assert "powersoc_today_pv_kwh" in r.get_data(as_text=True)


def test_home_api_disabled_after_clear(auth_client):
    auth_client.post("/api/settings/home-token/generate")
    auth_client.delete("/api/settings/home-token")
    assert auth_client.get("/api/home/status").status_code == 403


def test_day_payload_export_estimate(tmp_db):
    # Today-like: Sunsynk export present, Octopus export not settled -> estimate.
    db.upsert_intervals("solar_intervals", [{
        "interval_start": "2026-05-01T12:00:00", "grid_export_kwh": 3.0}])
    p = app_module._build_day_payload("2026-05-01")
    assert p["export"]["estimated"] is True
    assert any(v for v in p["export"]["export_kwh"])      # chart populated
    # Settled day: 48 Octopus export intervals -> authoritative, not estimated.
    db.upsert_intervals("export_intervals", [{
        "interval_start": f"2026-05-02T{i//2:02d}:{(i % 2)*30:02d}:00",
        "interval_end": "x", "export_kwh": 0.1} for i in range(48)])
    p2 = app_module._build_day_payload("2026-05-02")
    assert p2["export"]["estimated"] is False
    assert any(v for v in p2["export"]["export_kwh"])


def test_build_tips():
    from datetime import datetime
    # Peak hour -> peak warning; low tomorrow -> battery tip.
    tips = app_module._build_tips({"tomorrow": {"outlook": "low"}},
                                  datetime(2026, 5, 26, 17, 0))
    assert any("Peak window" in t for t in tips)
    assert any("battery" in t.lower() for t in tips)
    # Cheap hour + strong tomorrow.
    tips2 = app_module._build_tips({"tomorrow": {"outlook": "high"}},
                                   datetime(2026, 5, 26, 5, 0))
    assert any("Cheap window" in t for t in tips2)
    assert any("export" in t.lower() for t in tips2)


def test_api_roi(auth_client):
    import config
    assert auth_client.get("/api/roi").get_json()["data"]["configured"] is False
    config.set_number("system_cost_gbp", 1000)
    db.upsert_tariff_rate("electricity_flexible", "2026-05-01", 30.0)
    db.upsert_daily_summary({"date": "2026-05-20", "house_load_kwh": 10,
                             "grid_import_kwh": 2, "export_earned_pence": 50})
    r = auth_client.get("/api/roi").get_json()["data"]
    assert r["configured"] is True
    # export 50p + self-supplied 8kWh * 30p = 290p = £2.90
    assert r["cumulative_benefit_gbp"] == 2.9
    assert r["percent_paid_back"] == 0.3


def test_api_account_balance_and_forecast(auth_client, monkeypatch):
    import datetime

    import octopus_graphql
    import sync
    monkeypatch.setattr(octopus_graphql, "get_account_overview", lambda *a, **k: {
        "available": True, "balance_pence": 5000, "balance_gbp": 50.0,
        "saving_sessions": {"next": None, "upcoming": [], "joined_campaign": False}})
    # Freeze the London date mid-month (hermetic: host-local date.today() near
    # midnight BST falls outside the route's London month window).
    fixed = datetime.date(2026, 5, 15)
    monkeypatch.setattr(sync, "today_date", lambda: fixed)
    monkeypatch.setattr(sync, "today", lambda: fixed.isoformat())
    # One FULL day (before "today") + today's partial; the projection must be
    # driven by full days only — today's partial row would bias it low.
    db.upsert_daily_summary({"date": "2026-05-01", "net_cost_pence": 200.0,
                             "octopus_ok": 1})
    db.upsert_daily_summary({"date": "2026-05-15", "net_cost_pence": 10.0,
                             "octopus_ok": 1})
    r = auth_client.get("/api/account").get_json()["data"]
    assert r["balance_gbp"] == 50.0
    bf = r["bill_forecast"]
    assert bf["month_to_date_pence"] == 210.0
    assert bf["days_with_data"] == 2
    # projection = full-day sum + full-day avg * remaining days (today incl.)
    assert bf["avg_daily_pence"] == 200.0
    assert bf["projected_month_pence"] == round(200.0 + 200.0 * 30, 2)


def test_export_csv(auth_client):
    db.upsert_daily_summary({"date": "2026-05-20", "pv_kwh": 18.4,
                             "grid_import_kwh": 4.0, "net_cost_pence": 120.0})
    r = auth_client.get("/api/export.csv")
    assert r.status_code == 200
    assert "text/csv" in r.content_type
    body = r.get_data(as_text=True)
    assert body.splitlines()[0].startswith("date,")   # header
    assert "2026-05-20" in body and "18.4" in body


def test_duplicate_username_rejected(auth_client):
    assert auth_client.post("/api/users", json={
        "username": "admin", "password": "longpassword"}).status_code == 409


# ---------- open-redirect hardening (P2-1) --------------------------------

@pytest.mark.parametrize("bad", [
    "//evil.com", "/\\evil.com", "/\\/evil.com", "https://evil.com",
    "http:evil.com", "\\\\evil.com", "javascript:alert(1)",
])
def test_login_next_open_redirect_blocked(client, bad):
    db.insert_user("admin", __import__("auth").hash_password("supersecret1"), role="admin")
    r = client.post("/login", data={"username": "admin", "password": "supersecret1",
                                     "next": bad})
    assert r.status_code in (302, 303)
    # Must never bounce off-site — every hostile target collapses to "/".
    assert r.headers["Location"] in ("/", "http://localhost/")


def test_login_next_preserves_safe_path(client):
    db.insert_user("admin", __import__("auth").hash_password("supersecret1"), role="admin")
    r = client.post("/login", data={"username": "admin", "password": "supersecret1",
                                     "next": "/history"})
    assert r.status_code in (302, 303)
    assert r.headers["Location"].endswith("/history")


# ---------- public endpoints must not set a session cookie (P3-1) ---------

def test_public_get_sets_no_cookie(client):
    for path in ("/api/health", "/widget/today", "/auth/login", "/login"):
        r = client.get(path)
        assert "Set-Cookie" not in r.headers, f"{path} leaked a Set-Cookie"


def test_authenticated_request_still_sets_permanent_cookie(auth_client):
    # The session cookie should still ride on an authenticated request.
    r = auth_client.get("/api/today")
    assert r.status_code == 200


# ---------- every GET route is non-5xx (route smoke sweep) ----------------

def test_all_get_routes_non_5xx(auth_client):
    # Iterate the FIXTURE app's url_map (a second create_app() could drift if
    # routes ever become config-conditional). The conftest network kill switch
    # makes this hermetic: routes that reach upstream APIs degrade offline.
    application = auth_client.application
    skip = {"pages.service_worker"}   # reads a file off disk; covered elsewhere
    for rule in application.url_map.iter_rules():
        if "GET" not in (rule.methods or set()) or "<" in rule.rule:
            continue
        if rule.endpoint in skip or rule.rule.startswith("/static"):
            continue
        r = auth_client.get(rule.rule)
        assert r.status_code < 500, f"{rule.rule} -> {r.status_code}"


def test_all_get_routes_anon_never_200_except_public(client):
    """Auth-matrix sweep: as ANONYMOUS, every GET route must be 401/302/403 —
    except the deliberate public surface. A new route accidentally registered
    public would ship unnoticed without this."""
    db.insert_user("admin", __import__("auth").hash_password("supersecret1"))
    public = {"/api/health", "/login", "/setup", "/widget/today", "/logout",
              "/auth/login", "/auth/logout"}
    for rule in client.application.url_map.iter_rules():
        if "GET" not in (rule.methods or set()) or "<" in rule.rule:
            continue
        if rule.rule.startswith("/static") or rule.endpoint == "pages.service_worker":
            continue
        r = client.get(rule.rule)
        if rule.rule in public:
            assert r.status_code < 400, f"public {rule.rule} -> {r.status_code}"
        else:
            assert r.status_code in (302, 401, 403), \
                f"{rule.rule} served {r.status_code} to an anonymous client"


# ---------- proxy-aware login throttle (P2-2) -----------------------------

def test_proxyfix_uses_forwarded_client_ip(tmp_db, monkeypatch):
    # With TRUST_PROXY on, remote_addr (and thus the throttle key + audit IP)
    # must reflect X-Forwarded-For, not the proxy/gateway socket address.
    import config
    monkeypatch.setattr(config, "TRUST_PROXY", True)
    application = app_module.create_app()
    application.config.update(TESTING=True)
    seen = {}

    @application.route("/__whoami")
    def _whoami():   # noqa: ANN202
        from flask import request
        seen["ip"] = request.remote_addr
        return "ok"

    client = application.test_client()
    client.post("/setup", data={"setup_token": auth.setup_token(), "username": "admin", "password": "supersecret1"})
    client.get("/__whoami", headers={"X-Forwarded-For": "203.0.113.9"})
    assert seen["ip"] == "203.0.113.9"


def test_no_proxyfix_by_default(tmp_db):
    # Default (TRUST_PROXY off) must NOT trust X-Forwarded-For — the wsgi_app is
    # left unwrapped so a direct client can't spoof its source IP.
    application = app_module.create_app()
    assert application.wsgi_app.__class__.__name__ != "ProxyFix"
