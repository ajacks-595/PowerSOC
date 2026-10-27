"""Regression tests for the 2026-10-26 review: brief delivery and the
scheduler, plus delivery receipts (O2) and the primary/standby guard (O3).
Written to fail first. Hermetic per the project rules (see conftest)."""
from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
import requests

import app as app_module
import auth
import brief
import config
import database as db
import notifications
import planner
import sync

LONDON = ZoneInfo("Europe/London")


def _freeze(monkeypatch, iso):
    fixed = datetime.fromisoformat(iso).replace(tzinfo=LONDON)

    class _FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)
    monkeypatch.setattr(sync, "datetime", _FakeDT)
    return fixed


@pytest.fixture
def wired(monkeypatch):
    """A configured webhook + a stub post_brief returning queued results."""
    config.set_credential("MATTERMOST_WEBHOOK_URL", "https://mm.example/hooks/x")
    results, sent = [], []

    def fake_post(msg):
        sent.append(msg)
        return results.pop(0) if results else (True, "200 ok")
    monkeypatch.setattr(notifications, "post_brief", fake_post)
    monkeypatch.setattr(brief, "build_daily_brief",
                        lambda d, flex_rate_p=None, **k: f"brief-{d}")
    return results, sent


@pytest.fixture
def client():
    application = app_module.create_app()
    application.config.update(TESTING=True)
    return application.test_client()


def _admin(client):
    uid = db.insert_user("admin", auth.hash_password("supersecret1"), role="admin")
    with client.session_transaction() as s:
        s["user_id"] = uid
        s["epoch"] = 0
    return client


# ---------- failed deliveries are visible --------------------------------------

def test_a_failed_brief_is_recorded_and_logged_as_a_warning(monkeypatch, wired, caplog):
    results, _ = wired
    results.append((False, "302 redirected to x.cloudflareaccess.com"))
    monkeypatch.setattr(sync, "run_daily", lambda d=None: {"date": "2026-10-25", "errors": []})
    with caplog.at_level(logging.WARNING, logger="powersoc.sync"):
        sync.run_daily_and_notify()
    st = sync.brief_status()
    assert st["last_attempt"]["date"] == "2026-10-25"
    assert st["last_attempt"]["ok"] is False
    assert "cloudflareaccess" in st["last_attempt"]["detail"]
    assert "Brief NOT delivered" in caplog.text


def test_health_and_heartbeat_report_an_undelivered_brief(monkeypatch, wired, client):
    _freeze(monkeypatch, "2026-10-26T09:00:00")
    db.setting_set("last_brief_date", "2026-10-24")
    sync._record_brief("2026-10-25", False, "302 login wall", via="master")
    d = client.get("/api/health").get_json()["data"]
    assert d["brief_ok"] is False
    assert d["brief"]["last_delivered"] == "2026-10-24"
    pings = []
    monkeypatch.setattr(config, "HEARTBEAT_URL", "https://hc.example/abc")
    monkeypatch.setattr(requests, "get", lambda url, **k: pings.append(url))
    monkeypatch.setattr(sync, "ingest_ok", lambda: True)
    sync.heartbeat(sync.scheduler_ok())
    assert pings == ["https://hc.example/abc/fail"]


def test_brief_not_yet_due_is_not_a_failure(monkeypatch, wired):
    _freeze(monkeypatch, "2026-10-26T05:00:00")
    db.setting_set("last_brief_date", "2026-10-24")
    assert sync.brief_status()["ok"] is True       # due at 07:00, not yet


def test_no_webhook_means_brief_ok_is_none(monkeypatch):
    _freeze(monkeypatch, "2026-10-26T09:00:00")
    assert sync.brief_status()["ok"] is None


def test_safety_net_retry_says_what_actually_happened(monkeypatch, wired, caplog):
    results, _ = wired
    _freeze(monkeypatch, "2026-10-26T10:00:00")
    db.upsert_daily_summary({"date": "2026-10-25", "pv_kwh": 1.0, "sunsynk_ok": 1})
    db.setting_set("last_brief_date", "2026-10-24")
    sync._record_brief("2026-10-25", False, "302 login wall", via="master")
    results.append((False, "302 login wall"))
    with caplog.at_level(logging.INFO, logger="powersoc.sync"):
        sync.post_brief_if_ready("2026-10-25")
    assert "master likely missed" not in caplog.text
    assert "retrying" in caplog.text
    assert sync.brief_status()["last_attempt"]["attempts"] == 2


def test_a_brief_that_never_went_out_is_flagged_in_the_next_one(monkeypatch, wired):
    results, sent = wired
    monkeypatch.setattr(brief, "build_daily_brief",
                        lambda d, flex_rate_p=None, undelivered=None: f"brief-{d}" + (
                            f" | {undelivered['date']} wasn't delivered" if undelivered else ""))
    _freeze(monkeypatch, "2026-10-27T10:00:00")
    db.upsert_daily_summary({"date": "2026-10-26", "pv_kwh": 1.0, "sunsynk_ok": 1})
    db.setting_set("last_brief_date", "2026-10-24")
    sync._record_brief("2026-10-25", False, "302 login wall", via="catchup")
    assert sync.post_brief_if_ready("2026-10-26") is True
    assert "2026-10-25" in sent[-1] and "wasn't delivered" in sent[-1]


def test_the_note_is_rendered_by_the_real_brief_builder():
    text = brief.build_daily_brief("2026-10-26",
                                   undelivered={"date": "2026-10-25",
                                                "detail": "302 login wall"})
    assert "Sun 25 Oct" in text and "wasn't delivered" in text


# ---------- CLI re-send ------------------------------------------------------------

def test_cli_resend_of_an_old_date_never_moves_the_dedup_key_back(monkeypatch, wired):
    _, sent = wired
    db.setting_set("last_brief_date", "2026-10-25")
    monkeypatch.setattr(sync, "run_daily", lambda d=None: {"date": d, "errors": []})
    sync.run_daily_and_notify("2026-10-20")              # deliberate re-send
    assert db.setting_get("last_brief_date") == "2026-10-25"
    _freeze(monkeypatch, "2026-10-26T10:00:00")
    db.upsert_daily_summary({"date": "2026-10-25", "pv_kwh": 1.0})
    assert sync.post_brief_if_ready("2026-10-25") is False   # no duplicate
    assert len(sent) == 1


# ---------- resend + status in Settings ------------------------------------------

def test_resend_route_posts_and_records(client, wired):
    _admin(client)
    r = client.post("/api/brief/resend", json={"date": "2026-10-25"})
    d = r.get_json()["data"]
    assert r.status_code == 200 and d["sent"] is True
    st = client.get("/api/brief/status").get_json()["data"]
    assert st["last_attempt"]["date"] == "2026-10-25" and st["last_attempt"]["via"] == "resend"


def test_resend_validates_the_date(client, wired):
    _admin(client)
    assert client.post("/api/brief/resend", json={"date": "nope"}).status_code == 400


# ---------- the 06:25 charge plan ------------------------------------------------

def test_morning_plan_uses_the_typical_evening_soc_not_the_live_one(monkeypatch):
    """At 06:25 the battery has just finished the 04–07 cheap charge (~100%),
    so comparing live SoC with tonight's target always said "hold"."""
    from datetime import date, timedelta
    for back in range(1, 8):
        day = (date(2026, 10, 26) - timedelta(days=back)).isoformat()
        db.upsert_intervals("solar_intervals", [
            {"interval_start": f"{day}T21:30:00", "soc_pct": 30.0},
            {"interval_start": f"{day}T22:00:00", "soc_pct": 25.0}])
    monkeypatch.setattr(planner, "_now", lambda: datetime(2026, 10, 26, 6, 25, tzinfo=LONDON))
    import weather
    monkeypatch.setattr(weather, "get_forecast", lambda: {"tomorrow": {"outlook": "low"}})
    p = planner.get_plan(soc_pct=99.0, fetch_soc=False)
    assert p["recommended_action"] == "grid_charge"
    assert p["current_soc_pct"] == 25
    assert any("typical" in r for r in p["reasons"])
    # In the evening the live SoC is the right input.
    monkeypatch.setattr(planner, "_now", lambda: datetime(2026, 10, 26, 21, 0, tzinfo=LONDON))
    assert planner.get_plan(soc_pct=100.0, fetch_soc=False)["recommended_action"] == "hold"


# ---------- webhook delivery hygiene ---------------------------------------------

class _Reply:
    def __init__(self, status, text="", url="https://mm.example/hooks/x", history=()):
        self.status_code, self.text, self.url, self.history = status, text, url, list(history)


def test_webhook_posts_do_not_follow_redirects(monkeypatch):
    seen = {}

    def fake_post(url, json=None, timeout=None, allow_redirects=True):
        seen["follow"] = allow_redirects
        return _Reply(307, "", url)
    monkeypatch.setattr(requests, "post", fake_post)
    ok, detail = notifications.post_brief("hi", webhook_url="https://mm.example/hooks/x")
    assert seen["follow"] is False
    assert ok is False and "redirect" in detail


@pytest.mark.parametrize("body", [
    "﻿<!DOCTYPE html><html><title>Sign in</title>",
    "<!-- cf --><html><head><title>Sign in</title>",
])
def test_html_detection_survives_a_bom_or_a_comment(body):
    assert notifications._not_a_webhook_reply(_Reply(200, body), "https://mm.example/hooks/x")


def test_error_reply_bodies_are_not_logged(monkeypatch, caplog):
    monkeypatch.setattr(requests, "post", lambda url, **k: _Reply(400, "token=SECRET-IN-BODY"))
    with caplog.at_level(logging.WARNING, logger="powersoc.notifications"):
        ok, _ = notifications.post_brief("hi", webhook_url="https://mm.example/hooks/x")
    assert ok is False and "SECRET-IN-BODY" not in caplog.text


# ---------- O3: instance identity + primary/standby ---------------------------------

def test_health_names_the_instance_and_its_role(client):
    config.set_text("instance_name", "pi")
    d = client.get("/api/health").get_json()["data"]
    assert d["instance"] == {"name": "pi", "role": "primary"}


def test_a_standby_without_a_peer_never_posts(monkeypatch, wired):
    _, sent = wired
    config.set_text("instance_role", "standby")
    monkeypatch.setattr(sync, "run_daily", lambda d=None: {"date": "2026-10-25", "errors": []})
    res = sync.run_daily_and_notify()
    assert res["brief_sent"] is False and sent == []


def _peer(monkeypatch, body=None, exc=None):
    class R:
        status_code = 200

        def json(self):
            return {"success": True, "data": body}
    def fake_get(url, timeout=None, **k):
        assert url == "http://192.168.1.51:8073/api/health"
        if exc:
            raise exc
        return R()
    monkeypatch.setattr(sync.requests, "get", fake_get)


def test_standby_stays_quiet_while_the_primary_delivers(monkeypatch, wired):
    _, sent = wired
    _freeze(monkeypatch, "2026-10-26T08:00:00")
    config.set_text("instance_role", "standby")
    config.set_text("peer_url", "http://192.168.1.51:8073/")
    db.upsert_daily_summary({"date": "2026-10-25", "pv_kwh": 1.0})
    _peer(monkeypatch, {"instance": {"role": "primary"}, "brief_ok": True,
                        "brief": {"last_delivered": "2026-10-25"}})
    assert sync.post_brief_if_ready("2026-10-25") is False
    assert sent == []


def test_standby_takes_over_when_the_primary_fails(monkeypatch, wired):
    _, sent = wired
    _freeze(monkeypatch, "2026-10-26T08:00:00")
    config.set_text("instance_role", "standby")
    config.set_text("peer_url", "http://192.168.1.51:8073")
    db.upsert_daily_summary({"date": "2026-10-25", "pv_kwh": 1.0})
    _peer(monkeypatch, {"instance": {"role": "primary"}, "brief_ok": False,
                        "brief": {"last_delivered": "2026-10-24"}})
    assert sync.post_brief_if_ready("2026-10-25") is True
    assert "standby" in sent[-1]


def test_standby_takes_over_when_the_primary_is_unreachable(monkeypatch, wired):
    _, sent = wired
    _freeze(monkeypatch, "2026-10-26T08:00:00")
    config.set_text("instance_role", "standby")
    config.set_text("peer_url", "http://192.168.1.51:8073")
    db.upsert_daily_summary({"date": "2026-10-25", "pv_kwh": 1.0})
    _peer(monkeypatch, exc=requests.ConnectionError("down"))
    assert sync.post_brief_if_ready("2026-10-25") is True


def test_standby_suppresses_alerts_while_the_primary_is_up(monkeypatch, wired):
    import alerts
    _, sent = wired
    config.set_text("instance_role", "standby")
    config.set_text("peer_url", "http://192.168.1.51:8073")
    _peer(monkeypatch, {"instance": {"role": "primary"}, "brief_ok": True,
                        "brief": {"last_delivered": "2026-10-25"}})
    db.upsert_daily_summary({"date": sync.today(), "end_soc_pct": 3.0, "sunsynk_ok": 1})
    assert alerts.check_alerts(sync.today()) == []
    assert sent == []


def test_instance_settings_round_trip(client):
    _admin(client)
    r = client.post("/api/settings/instance", json={
        "instance_name": "pi", "instance_role": "standby",
        "peer_url": "http://192.168.1.50:8073"})
    assert r.status_code == 200
    d = client.get("/api/settings/instance").get_json()["data"]
    assert d == {"instance_name": "pi", "instance_role": "standby",
                 "peer_url": "http://192.168.1.50:8073"}
    assert client.post("/api/settings/instance",
                       json={"instance_role": "boss"}).status_code == 400
    assert client.post("/api/settings/instance",
                       json={"peer_url": "file:///etc/passwd"}).status_code == 400


def test_role_and_peer_can_come_from_the_environment(monkeypatch, wired):
    monkeypatch.setenv("POWERSOC_ROLE", "standby")
    monkeypatch.setenv("POWERSOC_PEER_URL", "http://192.168.1.51:8073/")
    assert config.instance_role() == "standby"
    assert config.peer_url() == "http://192.168.1.51:8073"
    config.set_text("instance_role", "primary")          # Settings wins
    assert config.instance_role() == "primary"


def test_standby_sends_alerts_while_the_primary_cannot_deliver(monkeypatch, wired):
    import alerts
    _, sent = wired
    config.set_text("instance_role", "standby")
    config.set_text("peer_url", "http://192.168.1.51:8073")
    _peer(monkeypatch, {"instance": {"role": "primary"}, "brief_ok": False,
                        "brief": {"last_delivered": "2026-10-24"}})
    db.upsert_daily_summary({"date": sync.today(), "end_soc_pct": 3.0, "sunsynk_ok": 1})
    assert "battery_low" in alerts.check_alerts(sync.today())

