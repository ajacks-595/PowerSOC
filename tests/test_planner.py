"""Tests for the battery charge planner (pure build_plan logic)."""
from __future__ import annotations

import planner


def test_low_solar_recommends_full_charge():
    p = planner.build_plan(tomorrow_outlook="low", current_soc_pct=40,
                           nameplate_kwh=10.0, cheap_rate_p=12.0)
    assert p["recommended_action"] == "grid_charge"
    assert p["target_soc_pct"] == 100
    # (100-40)% * 10 kWh stored / 0.94 charge efficiency = grid kWh drawn
    assert p["charge_kwh"] == 6.38
    assert p["est_cost_pence"] == 76.6            # 6.38 kWh * 12p
    assert "Low solar" in " ".join(p["reasons"])


def test_high_solar_recommends_no_charge():
    p = planner.build_plan(tomorrow_outlook="high", current_soc_pct=30,
                           nameplate_kwh=10.0)
    assert p["recommended_action"] == "no_charge"
    assert p["target_soc_pct"] == 0
    assert p["charge_kwh"] is None


def test_hold_when_already_above_target():
    p = planner.build_plan(tomorrow_outlook="moderate", current_soc_pct=85,
                           nameplate_kwh=10.0)
    assert p["recommended_action"] == "hold"      # 85 >= 70 target
    assert p["charge_kwh"] is None


def test_moderate_partial_topup():
    p = planner.build_plan(tomorrow_outlook="moderate", current_soc_pct=40,
                           nameplate_kwh=10.0)
    assert p["recommended_action"] == "grid_charge"
    assert p["target_soc_pct"] == 70
    assert p["charge_kwh"] == 3.19   # 3.0 kWh stored / 0.94 efficiency


def test_unknown_outlook_defaults_and_unsized_without_inputs():
    p = planner.build_plan(tomorrow_outlook=None, current_soc_pct=None,
                           nameplate_kwh=None)
    assert p["tomorrow_outlook"] == "unknown"
    assert p["target_soc_pct"] == 80
    assert p["charge_kwh"] is None                # no soc/nameplate -> can't size


def test_greenest_window_surfaced_in_reasons():
    p = planner.build_plan(tomorrow_outlook="low", current_soc_pct=50,
                           nameplate_kwh=10.0,
                           greenest={"from": "2026-05-28T02:00:00Z", "g": 80})
    assert any("Greenest grid" in r and "gCO₂" in r for r in p["reasons"])
