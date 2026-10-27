"""Regression tests for the 2026-10-26 review: upstream clients. Written to
fail first. Spring-forward integration (the 2026-10-07 rule, never
implemented), data gaps, empty series, sparse realtime payloads, token-cache
write failures, weather/carbon back-off and stale-data limits, account-keyed
GraphQL cache, CT fill of missing meter half-hours, a later-day Sunsynk
outage alert and CSV date normalisation. Hermetic (see conftest)."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest
import requests

import alerts
import app as app_module
import auth
import carbon
import config
import database as db
import sunsynk
import sync
import weather


def _clock_series(date_skip=None, repeat=None, kw=1.0):
    """5-minute clock-time records for a day: optionally skipping a local hour
    (spring) or repeating one (autumn), as the inverter reports them."""
    recs = []
    for h in range(24):
        if date_skip == h:
            continue
        passes = 2 if repeat == h else 1
        for _ in range(passes):
            for m in range(0, 60, 5):
                recs.append({"time": f"{h:02d}:{m:02d}", "value": kw})
    return recs


# ---------- DST: integrate on real instants -----------------------------------

def test_spring_forward_day_integrates_real_elapsed_time():
    """2027-03-28: 00:55 -> 02:00 is five real minutes, not 65 (+4.4%)."""
    recs = _clock_series(date_skip=1)
    real_h = 22 + 55 / 60
    assert sunsynk.integrate_series_kwh(recs, "kW", "2027-03-28") == pytest.approx(real_h, rel=0.01)
    pos, neg = sunsynk.integrate_signed_kwh(recs, "kW", "2027-03-28")
    assert pos == pytest.approx(real_h, rel=0.01) and neg == 0
    buckets = sunsynk.resample_halfhourly_energy(recs, "2027-03-28", "kW")
    assert sum(buckets.values()) == pytest.approx(real_h, rel=0.01)
    assert "2027-03-28T01:00" not in buckets


def test_autumn_day_still_counts_the_repeated_hour():
    recs = _clock_series(repeat=1)
    real_h = 24 + 55 / 60
    assert sunsynk.integrate_series_kwh(recs, "kW", "2026-10-25") == pytest.approx(real_h, rel=0.01)
    buckets = sunsynk.resample_halfhourly_energy(recs, "2026-10-25", "kW")
    assert sum(buckets.values()) == pytest.approx(real_h, rel=0.01)


def test_ordinary_day_is_unchanged():
    recs = _clock_series()
    assert sunsynk.integrate_series_kwh(recs, "kW", "2026-06-01") == pytest.approx(23 + 55 / 60, abs=1e-3)


def test_a_data_gap_is_spread_over_the_half_hours_it_spans():
    """Two records two hours apart dumped all 2 kWh into the 10:00 bucket."""
    recs = [{"time": "10:00", "value": 1.0}, {"time": "12:00", "value": 1.0}]
    b = sunsynk.resample_halfhourly_energy(recs, "2026-06-01", "kW")
    assert b == {f"2026-06-01T{t}": pytest.approx(0.5)
                 for t in ("10:00", "10:30", "11:00", "11:30")}
    pos, neg = sunsynk.resample_halfhourly_signed(
        [{"time": "10:00", "value": -1.0}, {"time": "11:00", "value": -1.0}], "2026-06-01")
    assert neg == {"2026-06-01T10:00": pytest.approx(0.5), "2026-06-01T10:30": pytest.approx(0.5)}


# ---------- Sunsynk payload shapes ------------------------------------------------

def test_series_labels_with_no_records_are_an_empty_day():
    c = sunsynk.SunsynkClient(username="u", password="p", plant_id="1")
    body = {"data": {"infos": [{"label": "PV", "unit": "kW", "records": []},
                               {"label": "SOC", "unit": "%", "records": []}]}}
    assert c._parse_day("2025-06-01", body).empty is True


def test_realtime_missing_sub_payload_is_unknown_not_zero(tmp_path):
    c = sunsynk.SunsynkClient(username="u", password="p", plant_id="1", inverter_sn="SN",
                              token_cache_path=str(tmp_path / "t.json"))

    def fake_get(path, params=None):
        if "realtime/input" in path:
            return {"data": {}}                       # PV sub-payload missing
        if "/grid/" in path:
            return {"data": {"pac": 250}}
        return {"data": {"power": -300, "soc": 55}}
    c._get = fake_get
    rt = c.get_realtime()
    assert rt["pv_w"] is None and rt["load_w"] is None
    assert rt["grid_w"] == 250 and rt["soc_pct"] == 55
    assert rt["today"]["pv_kwh"] is None


def test_token_cache_write_failure_does_not_break_login(tmp_path, monkeypatch, caplog):
    c = sunsynk.SunsynkClient(username="u", password="p", plant_id="1",
                              token_cache_path=str(tmp_path / "ro" / "t.json"))
    real_open = __import__("os").open

    def deny(path, *a, **k):
        if str(path).startswith(str(tmp_path / "ro")):
            raise PermissionError(13, "read-only")
        return real_open(path, *a, **k)
    monkeypatch.setattr(sunsynk.os, "open", deny)
    monkeypatch.setattr(sunsynk.tempfile, "mkstemp", lambda **k: (_ for _ in ()).throw(
        PermissionError(13, "read-only")))
    c._store_token_body({"data": {"access_token": "AT", "expires_in": 3600}})
    assert c._token == "AT"                         # usable for this process
    assert "token cache" in caplog.text


def test_concurrent_token_saves_use_distinct_temp_files(tmp_path, monkeypatch):
    names = []
    real_replace = sunsynk.os.replace
    monkeypatch.setattr(sunsynk.os, "replace",
                        lambda a, b: names.append(a) or real_replace(a, b))
    c = sunsynk.SunsynkClient(username="u", password="p", plant_id="1",
                              token_cache_path=str(tmp_path / "t.json"))
    c._store_token_body({"data": {"access_token": "A1", "expires_in": 3600}})
    c._store_token_body({"data": {"access_token": "A2", "expires_in": 3600}})
    assert len(set(names)) == 2


# ---------- weather: back-off, no head-of-line blocking, age limits ---------------

def test_weather_backs_off_after_a_failure(monkeypatch):
    calls = []
    monkeypatch.setattr(weather.requests, "get",
                        lambda *a, **k: calls.append(1) or (_ for _ in ()).throw(
                            requests.Timeout("slow")))
    assert weather.get_weather()["available"] is False
    assert weather.get_weather()["available"] is False
    assert weather.get_forecast()["available"] is False
    assert weather.get_forecast()["available"] is False
    assert len(calls) == 2                          # one per feed, then backed off


def test_weather_never_queues_behind_an_inflight_fetch(monkeypatch):
    """One slow Open-Meteo call held the shared lock for 10 s; every other
    request thread (incl. /api/health page loads) queued behind it."""
    monkeypatch.setattr(weather.requests, "get",
                        lambda *a, **k: pytest.fail("must not fetch while one is in flight"))
    with weather._cur_lock:
        t0 = time.monotonic()
        assert weather.get_weather()["available"] is False
        assert time.monotonic() - t0 < 0.5


def test_stale_weather_has_an_age_limit(monkeypatch):
    weather._cache.update(ts=time.time() - 4 * 3600,
                          data={"available": True, "location": "X", "temp_c": 9})
    monkeypatch.setattr(weather.requests, "get",
                        lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError()))
    assert weather.get_weather()["available"] is False


def test_yesterdays_forecast_is_not_served_as_today(monkeypatch):
    yday = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
    weather._fc_cache.update(ts=time.time() - 2 * 3600, data={
        "available": True, "today": {"date": yday, "outlook": "high"},
        "tomorrow": {"outlook": "low"}})
    monkeypatch.setattr(weather.requests, "get",
                        lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError()))
    assert weather.get_forecast()["available"] is False


# ---------- carbon: stale slots are re-read for "now" ----------------------------

def _slot(start, g):
    f = start.strftime("%Y-%m-%dT%H:%MZ")
    t = (start + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%MZ")
    return {"from": f, "to": t, "g": g, "index": "low"}


def test_stale_carbon_reports_the_slot_for_now_not_the_cached_current():
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    base = now - timedelta(minutes=now.minute % 30) - timedelta(hours=2)
    slots = [_slot(base + timedelta(minutes=30 * i), 100 + i) for i in range(12)]
    carbon._cache.update(ts=time.time() - 2 * 3600, fail_until=time.time() + 600, data={
        "available": True, "region": "South Scotland",
        "current": {"g": 100, "index": "low"}, "greenest": {"from": slots[0]["from"], "g": 100},
        "slots": slots})
    d = carbon.get_intensity()
    assert d["stale"] is True and d["current"]["g"] == 104
    assert d["greenest"]["from"] >= slots[4]["from"]          # never in the past


def test_carbon_slots_all_in_the_past_are_unavailable():
    base = datetime.now(timezone.utc) - timedelta(hours=30)
    slots = [_slot(base + timedelta(minutes=30 * i), 50) for i in range(6)]
    carbon._cache.update(ts=time.time() - 30 * 3600, fail_until=time.time() + 600, data={
        "available": True, "current": {"g": 50}, "greenest": {"g": 50}, "slots": slots})
    assert carbon.get_intensity()["available"] is False


# ---------- GraphQL cache keyed by account ----------------------------------------

def test_account_overview_cache_is_per_account(monkeypatch):
    import octopus_graphql as gql
    config.set_credential("OCTOPUS_API_KEY", "k")
    config.set_credential("OCTOPUS_ACCOUNT_NUMBER", "A-OLD")
    monkeypatch.setattr(gql, "_obtain_token", lambda key: "tok")
    monkeypatch.setattr(gql, "_build_overview", lambda tok, acc: {"available": True,
                                                                  "account": acc})
    assert gql.get_account_overview()["account"] == "A-OLD"
    config.set_credential("OCTOPUS_ACCOUNT_NUMBER", "A-NEW")
    assert gql.get_account_overview()["account"] == "A-NEW"


# ---------- CT fills the meter's missing half-hours -----------------------------

def test_missing_meter_half_hours_are_filled_from_the_ct():
    rows = []
    for i in range(46):                      # 23:00 and 23:30 not yet settled
        h, m = divmod(i * 30, 60)
        ts = f"2026-10-20T{h:02d}:{m:02d}:00+0100"
        rows.append({"interval_start": ts, "interval_end": ts, "consumption_kwh": 0.5,
                     "tariff_window": "day", "rate_p_per_kwh": 20.0, "cost_pence": 10.0})
    db.upsert_intervals("electricity_intervals", rows)
    db.upsert_intervals("solar_intervals", [
        {"interval_start": "2026-10-20T23:00:00", "grid_import_kwh": 1.0},
        {"interval_start": "2026-10-20T23:30:00", "grid_import_kwh": 1.0},
        {"interval_start": "2026-10-20T12:00:00", "grid_import_kwh": 9.0}])   # metered: ignored
    db.upsert_tariff_rate("electricity_import", "2026-10-19T23:00:00Z", 15.0,
                          "2026-10-20T23:00:00Z")
    s = sync.compute_daily_summary("2026-10-20")
    assert s["grid_import_kwh"] == pytest.approx(23.0 + 2.0)
    assert s["import_cost_pence"] == pytest.approx(460.0 + 30.0)
    assert s["data_partial"] == 1 and s["import_estimated"] == 1


# ---------- alerts: a Sunsynk outage later in the day -----------------------------

def test_sunsynk_offline_fires_when_todays_data_stopped(monkeypatch):
    s = {"sunsynk_ok": 1}
    settings = type("S", (), {"sunsynk_configured": True})()
    last = datetime(2026, 10, 26, 9, 30)
    keys = [k for k, _ in alerts.evaluate(s, settings, 15, last_solar_at=last)]
    assert "sunsynk_offline" in keys
    keys = [k for k, _ in alerts.evaluate(s, settings, 15,
                                          last_solar_at=datetime(2026, 10, 26, 14, 30))]
    assert "sunsynk_offline" not in keys


# ---------- CSV export normalises its dates ---------------------------------------

def test_csv_export_normalises_basic_format_dates():
    application = app_module.create_app()
    application.config.update(TESTING=True)
    c = application.test_client()
    uid = db.insert_user("admin", auth.hash_password("supersecret1"), role="admin")
    with c.session_transaction() as sess:
        sess["user_id"], sess["epoch"] = uid, 0
    db.upsert_daily_summary({"date": "2026-10-20", "octopus_ok": 1, "pv_kwh": 1.0})
    body = c.get("/api/export.csv?start=20261020&end=20261020").get_data(as_text=True)
    assert "2026-10-20" in body
