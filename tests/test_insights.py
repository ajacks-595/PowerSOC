"""Quick wins 1, 2, 5 — baseload tracker, battery time-to-reserve/full, and
the meter vs CT cross-check (insights.py). Hermetic (see conftest)."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

import app as app_module
import auth
import config
import database as db
import insights
import sync

LONDON = ZoneInfo("Europe/London")
TODAY = date(2026, 10, 22)   # clear of the 25 Oct DST day (50 half-hours)


def _solar_day(d: str, night_kwh=0.2, day_kwh=0.6, imp=0.1):
    rows = []
    for i in range(48):
        h, m = divmod(i * 30, 60)
        rows.append({"interval_start": f"{d}T{h:02d}:{m:02d}:00",
                     "house_load_kwh": night_kwh if h < 5 else day_kwh,
                     "grid_import_kwh": imp})
    db.upsert_intervals("solar_intervals", rows)


def _meter_day(d: str, kwh=0.1, rate=20.0):
    db.upsert_intervals("electricity_intervals", [
        {"interval_start": f"{d}T{i // 2:02d}:{i % 2 * 30:02d}:00+0000", "interval_end": "x",
         "consumption_kwh": kwh, "tariff_window": "day", "rate_p_per_kwh": rate,
         "cost_pence": kwh * rate} for i in range(48)])


# ---------- baseload ----------------------------------------------------------

def test_baseload_is_the_quiet_hours_draw_in_watts(monkeypatch):
    monkeypatch.setattr(insights, "_today", lambda: TODAY)
    for back in range(1, 11):
        d = (TODAY - timedelta(days=back)).isoformat()
        _solar_day(d, night_kwh=0.2)                 # 0.2 kWh / half-hour = 400 W
        _meter_day(d, rate=25.0)
        db.upsert_daily_summary(sync.compute_daily_summary(d))
    b = insights.baseload(days=30)
    assert b["available"] is True
    assert b["watts"] == pytest.approx(400, abs=1)
    assert b["annual_kwh"] == pytest.approx(400 * 8.766, rel=0.01)
    assert b["annual_cost_pence"] == pytest.approx(b["annual_kwh"] * 25.0, rel=0.01)
    assert len(b["trend"]) == 10


def test_baseload_needs_data():
    assert insights.baseload()["available"] is False


# ---------- battery ETA ----------------------------------------------------------

def test_battery_time_to_reserve_and_full():
    now = datetime(2026, 10, 26, 18, 0, tzinfo=LONDON)
    eta = insights.battery_eta(soc_pct=60, battery_w=-2000, nameplate_kwh=10,
                               reserve_pct=10, now=now)
    assert eta["kind"] == "reserve" and eta["minutes"] == 150           # 5 kWh at 2 kW
    assert eta["at"].startswith("2026-10-26T20:30")
    full = insights.battery_eta(soc_pct=80, battery_w=3000, nameplate_kwh=10,
                                reserve_pct=10, now=now)
    assert full["kind"] == "full" and full["minutes"] == 40
    assert insights.battery_eta(soc_pct=60, battery_w=20, nameplate_kwh=10,
                                reserve_pct=10, now=now) is None        # idle
    assert insights.battery_eta(soc_pct=None, battery_w=-2000, nameplate_kwh=10,
                                reserve_pct=10, now=now) is None
    assert insights.battery_eta(soc_pct=8, battery_w=-500, nameplate_kwh=10,
                                reserve_pct=10, now=now)["minutes"] == 0


def test_live_snapshot_carries_the_eta(monkeypatch):
    from routes import payloads
    config.set_number("battery_nameplate_kwh", 10)
    config.set_number("battery_reserve_pct", 20)
    monkeypatch.setattr(insights, "_now", lambda: datetime(2026, 10, 26, 18, 0, tzinfo=LONDON))
    snap = {"soc_pct": 50, "battery_w": -1500}
    eta = payloads.battery_eta_for(snap)
    assert eta["kind"] == "reserve" and eta["minutes"] == 120          # 3 kWh at 1.5 kW


def test_reserve_setting_is_validated():
    application = app_module.create_app()
    c = application.test_client()
    uid = db.insert_user("admin", auth.hash_password("supersecret1"), role="admin")
    with c.session_transaction() as s:
        s["user_id"], s["epoch"] = uid, 0
    assert c.post("/api/settings/system", json={"battery_reserve_pct": 15}).status_code == 200
    assert config.get_number("battery_reserve_pct") == 15
    assert c.post("/api/settings/system", json={"battery_reserve_pct": 140}).status_code == 400


# ---------- meter vs CT cross-check ------------------------------------------------

def test_ct_check_compares_complete_days_only(monkeypatch):
    monkeypatch.setattr(insights, "_today", lambda: TODAY)
    for back, ct in ((1, 0.103), (2, 0.102), (3, 0.150)):
        d = (TODAY - timedelta(days=back)).isoformat()
        _solar_day(d, imp=ct)
        _meter_day(d, kwh=0.1)
    partial = (TODAY - timedelta(days=4)).isoformat()
    _solar_day(partial, imp=0.2)
    db.upsert_intervals("electricity_intervals", [{"interval_start": f"{partial}T00:00:00+0000",
                        "interval_end": "x", "consumption_kwh": 0.1, "tariff_window": "day",
                        "rate_p_per_kwh": 20.0, "cost_pence": 2.0}])
    r = insights.ct_check(days=30)
    assert [d["date"] for d in r["days"]] == [(TODAY - timedelta(days=b)).isoformat() for b in (3, 2, 1)]
    assert r["median_ratio"] == pytest.approx(1.03)
    assert [d["date"] for d in r["flagged"]] == [(TODAY - timedelta(days=3)).isoformat()]
    assert "3.0% high" in r["summary"]
