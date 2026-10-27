"""Regression tests for the 2026-10-21 fixes.

After the 2026-09-30 volume loss the restored credentials no longer decrypted,
and for three weeks nothing ingested while every surface looked healthy:
health "ok", an hourly "ok" heartbeat, and an all-zero summary written every
day. Also: the CO2 panel never rendered because the default carbon postcode
("DG") is an AREA, which the NESO API answers with `null`. Hermetic per the
project rules (see conftest).
"""
from __future__ import annotations

import logging

import pytest
import requests

import app as app_module
import auth
import brief
import carbon
import config
import database as db
import sync

BASE = "http://192.168.1.50:8073"
GARBAGE = "gAAAAABnot-a-valid-fernet-token"


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


def _configure_octopus():
    config.set_credential("OCTOPUS_API_KEY", "sk_test")
    config.set_credential("OCTOPUS_ACCOUNT_NUMBER", "A-TEST")


def _store_unreadable(*keys):
    """What a key change leaves behind: ciphertext the current key can't open."""
    for k in keys:
        db.setting_set("cred:" + k, GARBAGE)


# ---------- unreadable credentials are surfaced ------------------------------

def test_unreadable_credentials_lists_only_undecryptable_keys(monkeypatch):
    _configure_octopus()
    _store_unreadable("SUNSYNK_PLANT_ID", "MATTERMOST_CHANNEL")
    assert config.unreadable_credentials() == ["SUNSYNK_PLANT_ID",
                                               "MATTERMOST_CHANNEL"]
    # An env-var fallback means the value still resolves — not a problem.
    monkeypatch.setenv("MATTERMOST_CHANNEL", "energy")
    assert config.unreadable_credentials() == ["SUNSYNK_PLANT_ID"]


def test_health_reports_unreadable_credentials_but_stays_200(client):
    r = client.get("/api/health")
    assert r.status_code == 200 and r.get_json()["data"]["credentials_ok"] is True
    _store_unreadable("OCTOPUS_API_KEY")
    r = client.get("/api/health")
    # 200: the container healthcheck must not restart-loop over config.
    assert r.status_code == 200 and r.get_json()["data"]["credentials_ok"] is False


def test_settings_api_flags_unreadable_fields(admin):
    _store_unreadable("SUNSYNK_PLANT_ID", "OCTOPUS_API_KEY")
    d = admin.get("/api/settings", base_url=BASE).get_json()["data"]
    assert d["unreadable"] == ["SUNSYNK_PLANT_ID", "OCTOPUS_API_KEY"]
    assert d["credentials"]["SUNSYNK_PLANT_ID"]["unreadable"] is True
    assert d["credentials"]["OCTOPUS_API_KEY"]["unreadable"] is True
    assert d["credentials"]["SUNSYNK_USERNAME"]["unreadable"] is False


def test_saving_null_clears_an_unreadable_value_even_for_secrets(admin):
    """The UI sends null for an unreadable field left blank. Without that,
    blank means "unchanged" and an optional field (the Mattermost channel)
    could never be cleared."""
    _store_unreadable("MATTERMOST_CHANNEL", "OCTOPUS_API_KEY")
    r = admin.post("/api/settings/credentials", base_url=BASE,
                   json={"MATTERMOST_CHANNEL": None, "OCTOPUS_API_KEY": None},
                   headers={"Origin": BASE})
    assert r.status_code == 200
    assert config.unreadable_credentials() == []
    assert db.setting_get("cred:OCTOPUS_API_KEY") is None


def test_settings_page_has_the_warning_slot(admin):
    html = admin.get("/settings", base_url=BASE).get_data(as_text=True)
    assert 'id="cred-warning"' in html


# ---------- nothing configured = nothing written -----------------------------

def test_unconfigured_run_daily_writes_nothing(caplog):
    with caplog.at_level(logging.WARNING, logger="powersoc.sync"):
        result = sync.run_daily("2026-10-20")
    assert result["skipped"] is True
    assert "no data source configured" in result["errors"]
    assert db.get_daily_summary("2026-10-20") is None
    assert "add one in Settings" in caplog.text


def test_run_daily_with_unreadable_credentials_says_why(caplog):
    _store_unreadable("OCTOPUS_API_KEY", "OCTOPUS_ACCOUNT_NUMBER")
    with caplog.at_level(logging.WARNING, logger="powersoc.sync"):
        sync.run_daily("2026-10-20")
    assert db.get_daily_summary("2026-10-20") is None
    assert "can't be decrypted" in caplog.text


def test_unconfigured_backfill_writes_nothing():
    status = sync.run_backfill(max_days=5)
    assert db.summary_date_bounds() == (None, None)
    assert "No data source" in (status["error"] or "")
    assert status["running"] is False


def test_backfill_logs_completion(monkeypatch, caplog):
    """There was no "finished" line: a backfill's end was invisible in logs."""
    _configure_octopus()
    monkeypatch.setattr(sync, "resolve_meters", lambda s, o: sync.octopus.MeterInfo())
    monkeypatch.setattr(sync, "sync_octopus_day", lambda *a, **k: {})
    monkeypatch.setattr(sync, "_standing_charges", lambda *a: (None, None))
    monkeypatch.setattr(sync.time, "sleep", lambda s: None)
    with caplog.at_level(logging.INFO, logger="powersoc.sync"):
        sync.run_backfill(max_days=3)
    assert "Backfill finished: 3 days" in caplog.text


def test_catchup_brief_is_quiet_without_a_webhook(monkeypatch, caplog):
    """It used to log "posting now" + a send failure every hour, forever."""
    from datetime import datetime as real_dt

    class _Frozen(real_dt):
        @classmethod
        def now(cls, tz=None):
            return real_dt(2026, 10, 21, 12, 0, tzinfo=tz)

    monkeypatch.setattr(sync, "datetime", _Frozen)
    db.upsert_daily_summary({"date": "2026-10-20", "pv_kwh": 1.0})
    db.setting_set("last_brief_date", "2026-10-19")
    with caplog.at_level(logging.INFO, logger="powersoc.sync"):
        assert sync.post_brief_if_ready("2026-10-20") is False
    assert "posting now" not in caplog.text
    assert "send failed" not in caplog.text


# ---------- heartbeat + brief say when data can't be flowing -----------------

def test_ingest_ok_states():
    assert sync.ingest_ok() is False                 # nothing configured
    _configure_octopus()
    assert sync.ingest_ok() is True
    _store_unreadable("MATTERMOST_CHANNEL")          # configured, but a stale key
    assert sync.ingest_ok() is False


def test_cron_heartbeat_fails_when_nothing_is_configured(tmp_path, monkeypatch):
    import sync_daily
    monkeypatch.setattr(config, "BACKUP_DIR", tmp_path / "b")
    monkeypatch.setattr(config, "HEARTBEAT_URL", "https://hc.example/abc")
    pings = []

    def fake_get(url, **k):   # the brief's weather/carbon calls stay offline
        if "hc.example" not in url:
            raise requests.ConnectionError("offline")
        pings.append(url)
    monkeypatch.setattr(requests, "get", fake_get)
    assert sync_daily.main(["sync_daily.py"]) == 0
    assert pings == ["https://hc.example/abc/fail"]


def test_brief_leads_with_a_config_warning():
    assert "No data source is configured" in brief.build_daily_brief("2026-10-20")
    _configure_octopus()
    assert "⚠️ **" not in brief.build_daily_brief("2026-10-20").split("\n")[2]
    _store_unreadable("SUNSYNK_PLANT_ID")
    text = brief.build_daily_brief("2026-10-20")
    assert "1 saved credential can't be decrypted" in text
    assert text.index("can't be decrypted") < text.index("Solar & Battery")


# ---------- carbon: postcode district, null body, failure caching ------------

# Trimmed from the live API response for DG1 on 2026-10-21 (incl. a genuine
# 0 g/kWh slot — windy South Scotland — which must not be treated as missing).
_LIVE_DG1 = {"data": {
    "regionid": 2, "dnoregion": "SP Distribution", "shortname": "South Scotland",
    "postcode": "DG1", "data": [
        {"from": "2026-10-21T17:30Z", "to": "2026-10-21T18:00Z",
         "intensity": {"forecast": 0, "index": "very low"}, "generationmix": []},
        {"from": "2026-10-21T18:00Z", "to": "2026-10-21T18:30Z",
         "intensity": {"forecast": 12, "index": "very low"}, "generationmix": []}]}}


class _Resp:
    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


@pytest.mark.parametrize("raw,expected", [
    ("dg1 2ab", "DG1"), (" sw1a 1aa ", "SW1A"), ("EH12", "EH12"),
    ("DG", "DG"), ("../x", "X"), ("", ""),
])
def test_normalise_outcode(raw, expected):
    assert carbon.normalise_outcode(raw) == expected


@pytest.mark.parametrize("stored,env,expected", [
    (None, None, "DG1"),        # the default is now a district
    ("DG", None, "DG1"),        # a bare area falls back to its "1" district
    (None, "RG", "RG1"),
    ("EH12", None, "EH12"),
])
def test_outcode_is_always_a_district(monkeypatch, stored, env, expected):
    if stored:
        config.set_text("carbon_outcode", stored)
    if env:
        monkeypatch.setenv("POWERSOC_CARBON_OUTCODE", env)
    assert carbon._outcode() == expected


def test_carbon_parses_live_shape_and_keeps_zero(monkeypatch):
    urls = []
    monkeypatch.setattr(carbon.requests, "get",
                        lambda url, **k: urls.append(url) or _Resp(_LIVE_DG1))
    d = carbon.get_intensity()
    assert urls[0].endswith("/fw24h/postcode/DG1")
    assert d["available"] is True and d["region"] == "South Scotland"
    assert d["current"]["g"] == 0 and d["greenest"]["g"] == 0


def test_carbon_null_body_is_reported_and_backed_off(monkeypatch, caplog):
    """200 + `null` (unknown district) used to surface only as "no slots",
    and every page load re-requested it (~36 failures/day)."""
    calls = []
    monkeypatch.setattr(carbon.requests, "get",
                        lambda url, **k: calls.append(url) or _Resp(None))
    with caplog.at_level(logging.WARNING, logger="powersoc.carbon"):
        assert carbon.get_intensity()["available"] is False
    assert "no data for postcode district 'DG1'" in caplog.text
    assert carbon.get_intensity()["available"] is False
    assert len(calls) == 1                       # cached failure, no re-request
    carbon._cache["fail_until"] = 0.0            # back-off elapsed
    carbon.get_intensity()
    assert len(calls) == 2


def _live_dg1_now():
    """_LIVE_DG1 re-based so its first slot covers the current half-hour —
    stale data is re-read for "now" (2026-10-26), so fixed dates would make
    this test depend on the wall clock."""
    import copy
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    start = now - timedelta(minutes=now.minute % 30)
    body = copy.deepcopy(_LIVE_DG1)
    for i, slot in enumerate(body["data"]["data"]):
        f = start + timedelta(minutes=30 * i)
        slot["from"] = f.strftime("%Y-%m-%dT%H:%MZ")
        slot["to"] = (f + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%MZ")
    return body


def test_carbon_failure_keeps_serving_stale_data(monkeypatch):
    live = _live_dg1_now()
    monkeypatch.setattr(carbon.requests, "get", lambda url, **k: _Resp(live))
    assert carbon.get_intensity()["available"] is True
    carbon._cache["ts"] = 0.0                    # TTL expired
    monkeypatch.setattr(carbon.requests, "get", lambda url, **k: _Resp(None))
    d = carbon.get_intensity()
    assert d["available"] is True and d["stale"] is True


@pytest.mark.parametrize("value,status,stored", [
    ("dg1 2ab", 200, "DG1"),
    ("EH12", 200, "EH12"),
    ("", 200, None),            # cleared -> default district
    ("DG", 400, None),          # an area alone is what broke the panel
    ("12AB", 400, None),
])
def test_settings_validates_carbon_district(admin, value, status, stored):
    r = admin.post("/api/settings/system", base_url=BASE,
                   json={"carbon_outcode": value}, headers={"Origin": BASE})
    assert r.status_code == status
    assert (config.get_text("carbon_outcode") or None) == stored
