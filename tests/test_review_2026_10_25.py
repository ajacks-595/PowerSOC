"""Regression tests for the 2026-10-25 Cloudron login loop.

Cloudron's App Proxy reserves /login and /logout on the app's domain for its
own sign-in wall. PowerSOC redirected anonymous visitors to /login?next=/, so
behind Cloudron every redirect landed on Cloudron's login page again: sign in,
reload, same page, forever. PowerSOC's own pages now live under /auth/; the
old paths stay as aliases for LAN bookmarks. Hermetic per the project rules.
"""
from __future__ import annotations

from urllib.parse import urlsplit

import pytest

import app as app_module
import auth
import database as db

# Paths Cloudron's proxy auth answers itself — PowerSOC must never send a
# browser to them, or the request never reaches the app.
CLOUDRON_RESERVED = {"/login", "/logout"}
PROXIED = "http://192.168.1.51:8073"       # the Host Cloudron's upstream sends


@pytest.fixture
def client():
    application = app_module.create_app()
    application.config.update(TESTING=True)
    return application.test_client()


@pytest.fixture
def auth_client(client):
    r = client.post("/setup", data={"setup_token": auth.setup_token(),
                                    "username": "admin",
                                    "password": "supersecret1"})
    assert r.status_code in (302, 303)
    return client


def _location_path(r) -> str:
    return urlsplit(r.headers["Location"]).path


def test_anonymous_redirects_never_target_cloudron_reserved_paths(client):
    db.insert_user("admin", auth.hash_password("supersecret1"))
    r = client.get("/")
    assert r.status_code == 302
    assert r.headers["Location"] == "/auth/login?next=/"
    for rule in client.application.url_map.iter_rules():
        if "GET" not in (rule.methods or set()) or "<" in rule.rule:
            continue
        if rule.rule.startswith("/static") or rule.endpoint == "pages.service_worker":
            continue
        r = client.get(rule.rule)
        if 300 <= r.status_code < 400:
            assert _location_path(r) not in CLOUDRON_RESERVED, \
                f"{rule.rule} redirects to {r.headers['Location']}"


def test_api_401_points_the_ui_at_auth_login(client):
    db.insert_user("admin", auth.hash_password("supersecret1"))
    r = client.get("/api/today")
    assert r.status_code == 401
    assert r.get_json()["redirect"] == "/auth/login"


def test_setup_after_first_run_redirects_to_auth_login(auth_client):
    auth_client.get("/auth/logout")
    r = auth_client.get("/setup")
    assert r.status_code == 302 and _location_path(r) == "/auth/login"


def test_login_page_posts_to_auth_login(client):
    html = client.get("/auth/login").get_data(as_text=True)
    assert 'action="/auth/login"' in html
    assert 'action="/login"' not in html


def test_login_through_the_proxy_on_the_canonical_path(client):
    """What a browser sends behind Cloudron: HTTPS, so Sec-Fetch-Site is set,
    and a Host header that may be the upstream rather than the public name."""
    db.insert_user("admin", auth.hash_password("supersecret1"))
    r = client.post("/auth/login", base_url=PROXIED,
                    data={"username": "admin", "password": "supersecret1",
                          "next": "/history"},
                    headers={"Sec-Fetch-Site": "same-origin",
                             "Origin": "https://powersoc.example.com"})
    assert r.status_code == 302 and _location_path(r) == "/history"
    assert client.get("/api/today", base_url=PROXIED).status_code == 200


def test_logout_link_and_redirect_use_auth_paths(auth_client):
    html = auth_client.get("/").get_data(as_text=True)
    assert 'href="/auth/logout"' in html and 'href="/logout"' not in html
    r = auth_client.get("/auth/logout")
    assert r.status_code == 302 and _location_path(r) == "/auth/login"
    assert auth_client.get("/api/today").status_code == 401


def test_old_paths_still_work_on_the_lan(client):
    db.insert_user("admin", auth.hash_password("supersecret1"))
    assert client.get("/login").status_code == 200
    r = client.post("/login", data={"username": "admin", "password": "supersecret1"})
    assert r.status_code == 302
    r = client.get("/logout")
    assert r.status_code == 302 and _location_path(r) == "/auth/login"
    assert client.get("/api/today").status_code == 401


def test_service_worker_knows_the_auth_paths(client):
    js = client.get("/sw.js").get_data(as_text=True)
    assert '"/auth/"' in js                      # never cached
    assert '"/auth/logout"' in js                # purges the cache on logout
