"""Features 9-12 (2026-10-26): battery economics, measured capacity / SoH,
the self-calibrating solar forecast with an under-performance alert, and the
grid outage / power-quality log. Hermetic (see conftest)."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import config
import database as db
import insights
import power_quality
import solar_forecast

LONDON = ZoneInfo("Europe/London")
DAY = "2026-10-20"


def _solar(rows):
    db.upsert_intervals("solar_intervals", [
        {"interval_start": f"{DAY}T{t}:00", "pv_kwh": 0.0, "battery_charge_kwh": 0.0,
         "battery_discharge_kwh": 0.0, "grid_import_kwh": 0.0, "grid_export_kwh": 0.0,
         **r} for t, r in rows])


def _import_rate(t, p):
    db.upsert_intervals("electricity_intervals", [{
        "interval_start": f"{DAY}T{t}:00+0100", "interval_end": "x", "consumption_kwh": 0.0,
        "tariff_window": "day", "rate_p_per_kwh": p, "cost_pence": 0.0}])


# ---------- 9. battery economics ------------------------------------------------

def test_battery_earnings_value_each_half_hour():
    _solar([("05:00", {"battery_charge_kwh": 1.0, "grid_import_kwh": 1.0}),     # grid charge
            ("12:00", {"battery_charge_kwh": 0.5, "pv_kwh": 2.0}),               # from solar
            ("18:00", {"battery_discharge_kwh": 1.0}),                           # avoids import
            ("20:00", {"battery_discharge_kwh": 0.4, "grid_export_kwh": 0.4})])  # exported
    for t, p in (("05:00", 7.5), ("12:00", 27.0), ("18:00", 27.0), ("20:00", 27.0)):
        _import_rate(t, p)
    db.upsert_intervals("export_intervals", [{
        "interval_start": f"{DAY}T{t}:00+0100", "interval_end": "x", "export_kwh": 0.0,
        "rate_p_per_kwh": 15.0, "earned_pence": 0.0} for t in ("12:00", "20:00")])
    day = insights.battery_day(DAY)
    assert day["value_p"] == pytest.approx(27.0 + 0.4 * 15.0)
    assert day["cost_p"] == pytest.approx(7.5 + 0.5 * 15.0)
    assert day["earned_p"] == pytest.approx(18.0)
    assert day["charge_grid_kwh"] == 1.0 and day["charge_pv_kwh"] == 0.5
    assert day["unpriced_slots"] == 0


def test_battery_economics_rolls_up_and_counts_cycles(monkeypatch):
    config.set_number("battery_nameplate_kwh", 2)
    monkeypatch.setattr(insights, "_today", lambda: date(2026, 10, 21))
    _solar([("18:00", {"battery_discharge_kwh": 1.0})])
    _import_rate("18:00", 30.0)
    e = insights.battery_economics()
    assert e["available"] is True and e["all_time_p"] == pytest.approx(30.0)
    assert e["cycles"] == pytest.approx(0.5) and e["per_kwh_p"] == pytest.approx(30.0)


# ---------- 10. measured capacity ------------------------------------------------

def test_capacity_estimated_from_a_soc_swing(monkeypatch):
    config.set_number("battery_nameplate_kwh", 10.65)
    rows = []
    for i in range(13):                       # 90% -> 30% over 12 half-hours, 6 kWh out
        h, m = divmod(17 * 60 + i * 30, 60)
        rows.append((f"{h:02d}:{m:02d}", {"soc_pct": 90 - i * 5,
                                          "battery_discharge_kwh": 0.5 if i else 0.0}))
    _solar(rows)
    est = insights.capacity_estimate(DAY)
    assert est["kwh"] == pytest.approx(10.0) and est["drop_pct"] == 60
    monkeypatch.setattr(insights, "_today", lambda: date(2026, 10, 21))
    h = insights.battery_health()
    assert h["trend_kwh"] == pytest.approx(10.0)
    assert h["soh_pct"] == pytest.approx(93.9, abs=0.1)


def test_small_swings_are_not_estimates():
    _solar([("17:00", {"soc_pct": 60}), ("17:30", {"soc_pct": 55, "battery_discharge_kwh": 0.5})])
    assert insights.capacity_estimate(DAY) is None


def test_bms_snapshot_is_recorded_once_a_day():
    snap = {"battery": {"capacity_ah": 200, "corrected_capacity_ah": 188.5, "voltage": 52.1,
                        "soc": 77, "bms_temp": 21.0, "total_discharge_kwh": 4200}}
    assert insights.record_battery_snapshot(snap, day=DAY) is True
    assert insights.record_battery_snapshot(snap, day=DAY) is False
    rows = insights.battery_snapshots()
    assert rows[0]["date"] == DAY and rows[0]["corrected_capacity_ah"] == 188.5


# ---------- 11. solar forecast ---------------------------------------------------

def _hist(days, ratio=2.0, rad=10.0, today=date(2026, 10, 21)):
    for back in range(1, days + 1):
        d = (today - timedelta(days=back)).isoformat()
        solar_forecast.store_radiation({d: rad})
        db.upsert_daily_summary({"date": d, "sunsynk_ok": 1, "pv_kwh": rad * ratio})


def test_forecast_is_calibrated_on_this_systems_history(monkeypatch):
    monkeypatch.setattr(solar_forecast, "_today", lambda: date(2026, 10, 21))
    _hist(10, ratio=2.0)
    f = solar_forecast.forecast_kwh({"today": {"radiation_mj": 6.0},
                                     "tomorrow": {"radiation_mj": 9.0}})
    assert f["k"] == pytest.approx(2.0)
    assert f["today_kwh"] == pytest.approx(12.0) and f["tomorrow_kwh"] == pytest.approx(18.0)


def test_forecast_needs_enough_history(monkeypatch):
    monkeypatch.setattr(solar_forecast, "_today", lambda: date(2026, 10, 21))
    _hist(3)
    assert solar_forecast.forecast_kwh({"tomorrow": {"radiation_mj": 9.0}})["tomorrow_kwh"] is None


def test_two_poor_days_raise_an_underperformance_alert(monkeypatch):
    monkeypatch.setattr(solar_forecast, "_today", lambda: date(2026, 10, 21))
    _hist(12, ratio=2.0)
    for d in ("2026-10-19", "2026-10-20"):
        db.upsert_daily_summary({"date": d, "sunsynk_ok": 1, "pv_kwh": 6.0})   # vs ~20 expected
    keys = solar_forecast.alert_candidates()
    assert keys and keys[0][0] == "solar_underperforming" and "-70%" in keys[0][1]
    db.upsert_daily_summary({"date": "2026-10-19", "sunsynk_ok": 1, "pv_kwh": 19.0})
    assert solar_forecast.alert_candidates() == []                # one poor day is weather


def test_radiation_history_is_fetched_and_stored(monkeypatch):
    class R:
        def raise_for_status(self):
            pass

        def json(self):
            return {"daily": {"time": ["2026-10-19", "2026-10-20"],
                              "shortwave_radiation_sum": [7.5, None]}}
    seen = {}
    monkeypatch.setattr(solar_forecast.requests, "get",
                        lambda url, params=None, **k: seen.update(params) or R())
    assert solar_forecast.refresh_history() == 1
    assert seen["past_days"] >= 30
    assert solar_forecast.radiation("2026-10-19") == 7.5


# ---------- 12. grid power quality --------------------------------------------------

def _rt(volt=240.0, hz=50.0, ok=True):
    return {"grid_v": volt, "grid_hz": hz, "grid_ok": ok}


def test_an_outage_is_logged_with_its_duration(monkeypatch):
    sent = []
    monkeypatch.setattr(power_quality, "_notify", lambda text: sent.append(text))
    t0 = datetime(2026, 10, 20, 18, 0, tzinfo=timezone.utc)
    power_quality.record(_rt(), now=t0)
    power_quality.record(_rt(volt=0, ok=False), now=t0 + timedelta(minutes=1))
    power_quality.record(_rt(volt=0, ok=False), now=t0 + timedelta(minutes=2))
    power_quality.record(_rt(), now=t0 + timedelta(minutes=9))
    ev = power_quality.events(days=7, now=t0 + timedelta(minutes=10))
    assert len(ev) == 1 and ev[0]["kind"] == "outage"
    assert ev[0]["minutes"] == pytest.approx(8.0)
    assert len(sent) == 2 and "outage" in sent[0].lower() and "8 min" in sent[1]


def test_voltage_and_frequency_excursions(monkeypatch):
    monkeypatch.setattr(power_quality, "_notify", lambda text: None)
    t0 = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
    power_quality.record(_rt(volt=255.1), now=t0)
    power_quality.record(_rt(volt=241.0), now=t0 + timedelta(minutes=1))
    power_quality.record(_rt(hz=49.3), now=t0 + timedelta(minutes=2))
    power_quality.record(_rt(), now=t0 + timedelta(minutes=3))
    kinds = sorted(e["kind"] for e in power_quality.events(days=7, now=t0 + timedelta(minutes=4)))
    assert kinds == ["frequency", "overvoltage"]
    s = power_quality.summary(days=7, now=t0 + timedelta(minutes=4))
    assert s["max_v"] == 255.1 and s["min_hz"] == 49.3 and s["samples"] == 4


def test_samples_are_rate_limited_and_pruned(monkeypatch):
    monkeypatch.setattr(power_quality, "_notify", lambda text: None)
    t0 = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
    power_quality.record(_rt(), now=t0)
    power_quality.record(_rt(), now=t0 + timedelta(seconds=20))   # same minute: skipped
    assert power_quality.summary(days=7, now=t0)["samples"] == 1
    old = t0 - timedelta(days=40)
    power_quality.record(_rt(), now=old)
    assert power_quality.prune(now=t0) == 1


def test_realtime_exposes_grid_voltage_and_state(tmp_path):
    import sunsynk
    c = sunsynk.SunsynkClient(username="u", password="p", plant_id="1", inverter_sn="SN",
                              token_cache_path=str(tmp_path / "t.json"))

    def fake_get(path, params=None):
        if "realtime/input" in path:
            return {"data": {"pac": 100}}
        if "/grid/" in path:
            return {"data": {"pac": 20, "fac": 50.03, "status": 1, "acRealyStatus": 1,
                             "vip": [{"volt": "248.5", "current": "1.5", "power": -349}]}}
        return {"data": {"power": 0, "soc": 50}}
    c._get = fake_get
    rt = c.get_realtime()
    assert rt["grid_v"] == 248.5 and rt["grid_hz"] == 50.03 and rt["grid_ok"] is True


def test_measured_capacity_stands_in_for_a_missing_nameplate(monkeypatch):
    monkeypatch.setattr(insights, "_today", lambda: date(2026, 10, 25))
    for d in ("2026-10-20", "2026-10-21", "2026-10-22"):
        rows = []
        for i in range(13):
            h, m = divmod(17 * 60 + i * 30, 60)
            rows.append({"interval_start": f"{d}T{h:02d}:{m:02d}:00", "soc_pct": 90 - i * 5,
                         "battery_discharge_kwh": 0.5 if i else 0.0, "battery_charge_kwh": 0.0})
        db.upsert_intervals("solar_intervals", rows)
    assert insights.effective_capacity_kwh() == pytest.approx(10.0)
    config.set_number("battery_nameplate_kwh", 12.0)
    assert insights.effective_capacity_kwh() == 12.0                # configured wins

