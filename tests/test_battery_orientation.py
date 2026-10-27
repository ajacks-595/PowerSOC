"""The signed /day Battery series: which sign is charging? The code assumed
+charge; the live inverter (2026-10-27) reports +discharge — every stored day
had charge and discharge swapped (overnight, SoC falling 91% -> 67%, stored as
0.55 kWh/half-hour of CHARGING). The orientation is now inferred per day from
the SoC itself, and stored history is repaired the same way."""
from __future__ import annotations

import pytest

import database as db
import sunsynk
import sync


def _body(battery_w: str, soc_from: int, soc_to: int):
    recs = lambda v: [{"time": f"{h:02d}:{m:02d}", "value": v}   # noqa: E731
                      for h in range(0, 3) for m in range(0, 60, 5)]
    soc = [{"time": f"{h:02d}:{m:02d}", "value": soc_from + (soc_to - soc_from) * (h * 60 + m) / 175}
           for h in range(0, 3) for m in range(0, 60, 5)]
    return {"data": {"infos": [
        {"label": "PV", "unit": "W", "records": recs("0")},
        {"label": "Battery", "unit": "W", "records": recs(battery_w)},
        {"label": "Load", "unit": "W", "records": recs("1000")},
        {"label": "SOC", "unit": "%", "records": soc}]}}


@pytest.mark.parametrize("battery_w", ["1000", "-1000"])
def test_discharge_is_whatever_coincides_with_soc_falling(battery_w):
    c = sunsynk.SunsynkClient(username="u", password="p", plant_id="1")
    r = c._parse_day("2026-10-24", _body(battery_w, 90, 60))       # SoC falling
    assert r.battery_discharge_kwh == pytest.approx(2.9, abs=0.1)
    assert r.battery_charge_kwh == pytest.approx(0.0)
    assert sum(row["battery_discharge_kwh"] or 0 for row in r.intervals) == pytest.approx(2.9, abs=0.1)
    up = c._parse_day("2026-10-24", _body(battery_w, 40, 70))      # SoC rising
    assert up.battery_charge_kwh == pytest.approx(2.9, abs=0.1) and up.battery_discharge_kwh == 0


def _store_day(d, swapped: bool):
    rows = []
    for i in range(12):                      # SoC falls 90 -> 35 while discharging 0.5/half-hour
        h, m = divmod(i * 30, 60)
        out = {"battery_discharge_kwh": 0.5, "battery_charge_kwh": 0.0}
        if swapped:
            out = {"battery_discharge_kwh": 0.0, "battery_charge_kwh": 0.5}
        rows.append({"interval_start": f"{d}T{h:02d}:{m:02d}:00", "soc_pct": 90 - i * 5, **out})
    db.upsert_intervals("solar_intervals", rows)
    db.upsert_daily_summary(sync.compute_daily_summary(d))


def test_stored_history_with_swapped_columns_is_repaired_once():
    _store_day("2026-10-20", swapped=True)
    _store_day("2026-10-21", swapped=False)
    assert sync.fix_battery_orientation() == 1
    for d in ("2026-10-20", "2026-10-21"):
        rows = db.intervals_for_date("solar_intervals", d)
        assert sum(r["battery_discharge_kwh"] for r in rows) == pytest.approx(6.0)
        assert sum(r["battery_charge_kwh"] for r in rows) == pytest.approx(0.0)
        s = db.get_daily_summary(d)
        assert s["battery_discharge_kwh"] == pytest.approx(6.0) and s["battery_charge_kwh"] == 0
    assert sync.fix_battery_orientation() == 0          # idempotent
