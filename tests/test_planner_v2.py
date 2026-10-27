"""Feature 13 — charge planner v2: a cost-minimising simulation over the
next 30 hours (typical load, calibrated solar, stored prices, reserve and
charge-rate limits) instead of a fixed target SoC per solar outlook."""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

import config
import database as db
import planner

LONDON = ZoneInfo("Europe/London")
NOW = datetime(2026, 10, 20, 20, 0, tzinfo=LONDON)


def _slots(n=60):
    return [NOW + timedelta(minutes=30 * i) for i in range(n)]


def _go_prices(slots):
    """Go-like: 7.5p 00:30-05:30, 27p otherwise."""
    return [7.5 if (t.hour, t.minute) >= (0, 30) and t.hour < 5 or (t.hour == 5 and t.minute == 0)
            else 27.0 for t in slots]


def test_cheap_night_charge_covers_tomorrows_load():
    slots = _slots()
    p = planner.simulate_plan(
        slots=slots, soc_pct=20, nameplate_kwh=10, reserve_pct=10, charge_kw=3.0,
        load=[0.5] * len(slots), pv=[0.0] * len(slots),
        import_p=_go_prices(slots), export_p=[15.0] * len(slots))
    assert p["recommended_action"] == "grid_charge"
    assert p["charge_kwh"] > 5
    assert all(s["price"] == 7.5 for s in p["charge_slots"])
    assert p["saving_pence"] > 0
    assert p["trajectory"][0]["soc"] == pytest.approx(20, abs=0.5)
    assert max(t["soc"] for t in p["trajectory"]) <= 100.0
    assert min(t["soc"] for t in p["trajectory"]) >= 10.0 - 1e-6


def test_no_charge_when_solar_will_fill_the_battery():
    slots = _slots()
    pv = [1.5 if 8 <= t.hour < 15 else 0.0 for t in slots]
    # Export at 4p: storing 7.5p grid energy to push solar out at 4p loses
    # money. (At 15p it would be genuine arbitrage, and the plan says so.)
    p = planner.simulate_plan(
        slots=slots, soc_pct=90, nameplate_kwh=10, reserve_pct=10, charge_kw=3.0,
        load=[0.1] * len(slots), pv=pv, import_p=_go_prices(slots),
        export_p=[4.0] * len(slots))
    assert p["recommended_action"] in ("hold", "no_charge") and p["charge_kwh"] == 0


def test_charging_to_export_solar_is_suggested_when_it_pays():
    """Go at 7.5p with a 15p export: charging overnight displaces solar into
    export at 15p — real arbitrage, so the plan recommends it."""
    slots = _slots()
    pv = [1.5 if 8 <= t.hour < 15 else 0.0 for t in slots]
    p = planner.simulate_plan(
        slots=slots, soc_pct=90, nameplate_kwh=10, reserve_pct=10, charge_kw=3.0,
        load=[0.1] * len(slots), pv=pv, import_p=_go_prices(slots),
        export_p=[15.0] * len(slots))
    assert p["recommended_action"] == "grid_charge" and p["saving_pence"] > 0


def test_charging_is_rate_limited_per_half_hour():
    slots = _slots()
    p = planner.simulate_plan(
        slots=slots, soc_pct=10, nameplate_kwh=20, reserve_pct=10, charge_kw=2.0,
        load=[0.6] * len(slots), pv=[0.0] * len(slots),
        import_p=_go_prices(slots), export_p=[0.0] * len(slots))
    assert all(s["kwh"] <= 1.0 + 1e-9 for s in p["charge_slots"])    # 2 kW × 0.5 h


def test_unknown_prices_are_never_charged_in():
    slots = _slots()
    prices = [None if 0 <= t.hour < 6 else 27.0 for t in slots]
    p = planner.simulate_plan(
        slots=slots, soc_pct=20, nameplate_kwh=10, reserve_pct=10, charge_kw=3.0,
        load=[0.5] * len(slots), pv=[0.0] * len(slots), import_p=prices,
        export_p=[15.0] * len(slots))
    assert all(s["price"] is not None for s in p["charge_slots"])


def test_get_plan_uses_the_simulation_when_history_allows(monkeypatch):
    config.set_number("battery_nameplate_kwh", 10)
    for back in range(1, 15):
        d = (NOW.date() - timedelta(days=back)).isoformat()
        db.upsert_intervals("solar_intervals", [
            {"interval_start": f"{d}T{i // 2:02d}:{i % 2 * 30:02d}:00",
             "house_load_kwh": 0.5, "pv_kwh": 0.0, "soc_pct": 30.0} for i in range(48)])
    start = datetime(2026, 10, 19, 23, 0)
    for i in range(4 * 48):
        t = start + timedelta(minutes=30 * i)
        local = t.replace(tzinfo=ZoneInfo("UTC")).astimezone(LONDON)
        price = 7.5 if (local.hour, local.minute) >= (0, 30) and local.hour < 5 else 27.0
        db.upsert_tariff_rate("electricity_import", t.strftime("%Y-%m-%dT%H:%M:%SZ"), price,
                              (t + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    monkeypatch.setattr(planner, "_now", lambda: NOW)
    import weather
    monkeypatch.setattr(weather, "get_forecast", lambda: {"tomorrow": {"outlook": "low"}})
    p = planner.get_plan(soc_pct=25.0, fetch_soc=False)
    assert p["method"] == "simulated"
    assert p["recommended_action"] == "grid_charge" and p["charge_slots"]
    assert p["headline"].startswith("Grid-charge")


def test_get_plan_falls_back_to_the_heuristic_without_history(monkeypatch):
    monkeypatch.setattr(planner, "_now", lambda: NOW)
    p = planner.get_plan(soc_pct=50.0, fetch_soc=False)
    assert p["method"] == "heuristic"


def test_headline_lists_each_charge_window():
    slots = _slots()
    cheap = {(4, 0), (4, 30), (13, 0), (13, 30)}
    prices = [7.5 if (t.hour, t.minute) in cheap else 30.0 for t in slots]
    p = planner.simulate_plan(
        slots=slots, soc_pct=15, nameplate_kwh=10, reserve_pct=10, charge_kw=3.0,
        load=[0.5] * len(slots), pv=[0.0] * len(slots), import_p=prices,
        export_p=[0.0] * len(slots))
    # NOW is 20:00, so these windows are on the next day.
    assert "tomorrow 04:00–05:00, tomorrow 13:00–14:00" in p["headline"]
    assert p["charge_window"] == "tomorrow 04:00–05:00, tomorrow 13:00–14:00"


def test_a_short_pause_at_the_same_price_does_not_split_the_window():
    slots = _slots()
    idx_slots = {8, 9, 11, 13}                    # 00:00, 00:30, 01:30, 02:30
    prices = [7.5 if i in idx_slots or i in (10, 12) else 30.0 for i in range(len(slots))]
    p = planner.simulate_plan(
        slots=slots, soc_pct=15, nameplate_kwh=4, reserve_pct=10, charge_kw=3.0,
        load=[0.5 if i not in (10, 12) else 0.0 for i in range(len(slots))],
        pv=[0.0] * len(slots), import_p=prices, export_p=[0.0] * len(slots))
    assert p["charge_window"].count("–") == 1


def test_fixed_tariffs_repeat_yesterdays_prices_for_unpublished_slots():
    slots = _slots()
    known = [27.0 if t.date() == slots[0].date() else None for t in slots]
    yesterday = lambda t: 13.0 if t.hour < 6 else 28.0          # noqa: E731
    filled = planner.fill_ahead(slots, known, "E-1R-COSY-22-12-08-N", lookup=yesterday)
    assert all(p is not None for p in filled)
    assert filled[10] == 13.0                                   # 01:00 next day
    assert planner.fill_ahead(slots, known, "E-1R-AGILE-24-10-01-N", lookup=yesterday) == known

