"""Quick win 3 — alert thresholds, quiet hours and the heartbeat URL are
editable in Settings (they were environment variables only)."""
from __future__ import annotations

import pytest
import requests

import app as app_module
import auth
import config
import database as db
import sync

LAN = {"REMOTE_ADDR": "10.0.0.50"}


@pytest.fixture
def admin():
    application = app_module.create_app()
    c = application.test_client()
    uid = db.insert_user("admin", auth.hash_password("supersecret1"), role="admin")
    with c.session_transaction() as s:
        s["user_id"], s["epoch"] = uid, 0
    return c


def test_defaults_come_from_the_environment(monkeypatch):
    monkeypatch.setattr(config, "ALERT_BATTERY_LOW_PCT", 22.0)
    assert config.alert_config()["battery_low_pct"] == 22.0
    config.set_number("alert_battery_low_pct", 12)
    assert config.alert_config()["battery_low_pct"] == 12.0      # Settings wins


def test_alert_settings_round_trip_and_validate(admin):
    r = admin.post("/api/settings/alerts", environ_base=LAN, json={
        "enabled": False, "battery_low_pct": 20, "high_cost_gbp": 4.5,
        "peak_kwh": 3, "quiet_hours": "22-7",
        "heartbeat_url": "https://hc.example/ping/abc"})
    assert r.status_code == 200
    d = admin.get("/api/settings/alerts", environ_base=LAN).get_json()["data"]
    assert d["enabled"] is False and d["battery_low_pct"] == 20
    assert d["high_cost_gbp"] == 4.5 and d["quiet_hours"] == "22-7"
    assert d["heartbeat_set"] is True and "heartbeat_url" not in d     # a secret
    assert config.alert_config()["high_cost_pence"] == 450
    for bad in ({"quiet_hours": "late"}, {"battery_low_pct": 150},
                {"heartbeat_url": "ftp://x"}, {"peak_kwh": "1e999"}):
        assert admin.post("/api/settings/alerts", json=bad,
                          environ_base=LAN).status_code == 400, bad


def test_alerts_use_the_ui_thresholds(monkeypatch):
    import alerts
    config.set_number("alert_battery_low_pct", 40)
    keys = [k for k, _ in alerts.evaluate({"end_soc_pct": 35}, object(), 10)]
    assert "battery_low" in keys


def test_disabling_alerts_in_settings_stops_them(monkeypatch):
    import alerts
    config.set_credential("MATTERMOST_WEBHOOK_URL", "https://mm.example/hooks/x")
    config.set_text("alerts_enabled", "0")
    db.upsert_daily_summary({"date": sync.today(), "end_soc_pct": 1.0, "sunsynk_ok": 1})
    assert alerts.check_alerts(sync.today()) == []


def test_heartbeat_uses_the_settings_url(monkeypatch):
    pings = []
    monkeypatch.setattr(requests, "get", lambda url, **k: pings.append(url))
    config.set_credential("HEARTBEAT_URL", "https://hc.example/ping/ui")
    sync.heartbeat(False)
    assert pings == ["https://hc.example/ping/ui/fail"]


def test_heartbeat_test_button(admin, monkeypatch):
    pings = []

    class R:
        status_code = 200
    monkeypatch.setattr(requests, "get", lambda url, **k: pings.append(url) or R())
    config.set_credential("HEARTBEAT_URL", "https://hc.example/ping/ui")
    r = admin.post("/api/settings/alerts/test-heartbeat", json={}, environ_base=LAN)
    assert r.get_json()["data"]["ok"] is True and pings == ["https://hc.example/ping/ui"]
