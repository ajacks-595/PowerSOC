"""O1 — per-consumer home-API tokens (2026-10-26). Each consumer (a home
dashboard, Home Assistant, Prometheus) gets its own token, stored hashed, with last-seen /
request counters; rejected tokens are tracked by fingerprint (never the
token) with a rate-limited log line and a daily alert — a heating controller sent a
wrong token ~1,000 times a day for weeks and only a WARNING flood showed it."""
from __future__ import annotations

import logging

import pytest

import app as app_module
import auth
import config
import database as db
import home_tokens

LAN = {"REMOTE_ADDR": "10.0.0.50"}


@pytest.fixture
def client():
    application = app_module.create_app()
    application.config.update(TESTING=True)
    c = application.test_client()
    db.insert_user("admin", auth.hash_password("supersecret1"), role="admin")
    return c


def _admin(c):
    uid = db.get_user_by_username("admin")["id"]
    with c.session_transaction() as s:
        s["user_id"], s["epoch"] = uid, 0
    return c


def _get(c, token, path="/api/home/status", ip="172.19.0.1"):
    return c.get(path, headers={"X-PowerSOC-Token": token} if token else {},
                 environ_base={"REMOTE_ADDR": ip})


def test_each_consumer_has_its_own_token_and_stats(client):
    jn = home_tokens.create("home-dashboard")
    vh = home_tokens.create("Home Assistant")
    assert _get(client, jn["token"]).status_code == 200
    assert _get(client, vh["token"], ip="172.19.0.7").status_code == 200
    rows = {c["name"]: c for c in home_tokens.list_consumers()}
    assert rows["home-dashboard"]["requests"] == 1 and rows["home-dashboard"]["last_ip"] == "172.19.0.1"
    assert rows["Home Assistant"]["last_ip"] == "172.19.0.7" and rows["Home Assistant"]["last_seen_at"]
    home_tokens.revoke(rows["Home Assistant"]["id"])
    assert _get(client, vh["token"]).status_code == 401
    assert _get(client, jn["token"]).status_code == 200


def test_tokens_are_stored_hashed(client):
    tok = home_tokens.create("prometheus")["token"]
    with db.conn() as c:
        dump = " ".join(str(v) for row in c.execute("SELECT * FROM home_consumers") for v in row)
    assert tok not in dump


def test_the_legacy_shared_token_still_works_and_is_listed(client):
    legacy = auth.home_api_generate_token()
    assert _get(client, legacy).status_code == 200
    names = [c["name"] for c in home_tokens.list_consumers()]
    assert any("legacy" in n for n in names)


def test_no_token_configured_at_all_is_a_403(client):
    assert _get(client, "anything").status_code == 403


def test_rejected_tokens_are_tracked_by_fingerprint_and_logged_rarely(client, caplog):
    home_tokens.create("home-dashboard")
    with caplog.at_level(logging.WARNING, logger="powersoc.auth"):
        for _ in range(50):
            assert _get(client, "wrong-token-123", path="/api/home/tariffs").status_code == 401
    warnings = [r for r in caplog.records if "home-API" in r.getMessage()]
    assert len(warnings) == 1                              # not 50
    rej = home_tokens.rejections()
    assert len(rej) == 1
    fp = home_tokens.fingerprint("wrong-token-123")
    assert rej[0]["fingerprint"] == fp and rej[0]["count_today"] == 50
    assert rej[0]["last_ip"] == "172.19.0.1" and rej[0]["last_path"] == "/api/home/tariffs"
    assert "wrong-token-123" not in str(rej)


def test_persistent_rejections_raise_one_alert_a_day(client, monkeypatch):
    import alerts
    import notifications
    home_tokens.create("home-dashboard")
    config.set_credential("MATTERMOST_WEBHOOK_URL", "https://mm.example/hooks/x")
    sent = []
    monkeypatch.setattr(notifications, "post_brief", lambda msg: sent.append(msg) or (True, "ok"))
    for _ in range(home_tokens.ALERT_AFTER):
        _get(client, "stale-token")
    home_tokens.flush()
    import sync
    db.upsert_daily_summary({"date": sync.today(), "sunsynk_ok": 1, "octopus_ok": 1})
    alerts.check_alerts(sync.today())
    alerts.check_alerts(sync.today())
    hits = [m for m in sent if "invalid home-API token" in m]
    assert len(hits) == 1 and "172.19.0.1" in hits[0]


def test_settings_api_manages_consumers(client):
    _admin(client)
    r = client.post("/api/settings/home-consumers", json={"name": "Home Assistant"}, environ_base=LAN)
    d = r.get_json()["data"]
    assert r.status_code == 200 and d["token"] and d["fingerprint"]
    listing = client.get("/api/settings/home-consumers", environ_base=LAN).get_json()["data"]
    assert listing["consumers"][0]["name"] == "Home Assistant"
    assert "token" not in listing["consumers"][0]
    cid = listing["consumers"][0]["id"]
    assert client.delete(f"/api/settings/home-consumers/{cid}", environ_base=LAN).status_code == 200
    assert client.get("/api/settings/home-consumers", environ_base=LAN).get_json()["data"]["consumers"] == []
    assert client.post("/api/settings/home-consumers", json={"name": ""},
                       environ_base=LAN).status_code == 400
