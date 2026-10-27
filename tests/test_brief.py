"""Tests for the daily brief builder + notable-event detection."""
from __future__ import annotations

import pytest

import brief
import carbon
import database as db
import octopus_graphql
import weather


@pytest.fixture(autouse=True)
def _no_outlook_net(monkeypatch):
    # Keep brief tests offline/deterministic (the forward-looking sections call
    # weather + carbon + Kraken GraphQL). Tests that want them stub explicitly.
    monkeypatch.setattr(weather, "get_forecast", lambda: {})
    monkeypatch.setattr(carbon, "get_intensity", lambda: {})
    monkeypatch.setattr(octopus_graphql, "get_account_overview", lambda: {})

# ---------- notable events -------------------------------------------------

def test_notable_battery_full_and_net_exporter():
    s = {"peak_soc_pct": 100.0, "end_soc_pct": 62, "grid_import_kwh": 2,
         "grid_export_kwh": 7, "peak_import_kwh": 0.3, "gas_kwh": 5,
         "sunsynk_ok": 1, "octopus_ok": 1}
    n = brief.detect_notable(s)
    assert any("100%" in x for x in n)
    assert any("Net exporter" in x for x in n)


def test_notable_high_peak_and_depleted():
    s = {"peak_soc_pct": 40, "end_soc_pct": 5, "grid_import_kwh": 8,
         "grid_export_kwh": 0, "peak_import_kwh": 3.2, "gas_kwh": 5,
         "sunsynk_ok": 1, "octopus_ok": 1}
    n = brief.detect_notable(s)
    assert any("High peak" in x for x in n)
    assert any("depleted" in x for x in n)


def test_notable_high_gas_vs_average():
    s = {"peak_soc_pct": 50, "end_soc_pct": 50, "grid_import_kwh": 4,
         "grid_export_kwh": 1, "peak_import_kwh": 0.5, "gas_kwh": 30,
         "sunsynk_ok": 1, "octopus_ok": 1}
    assert any("High gas" in x for x in brief.detect_notable(s, gas_30d_avg=10.0))
    # Not flagged when within range.
    assert not any("High gas" in x for x in brief.detect_notable(s, gas_30d_avg=25.0))


def test_notable_missing_sources():
    s = {"sunsynk_ok": 0, "octopus_ok": 0, "grid_import_kwh": 0,
         "grid_export_kwh": 0, "peak_import_kwh": 0, "gas_kwh": 0}
    n = brief.detect_notable(s)
    assert any("No data from Sunsynk" in x for x in n)
    assert any("No data from Octopus" in x for x in n)


# ---------- full brief assembly -------------------------------------------

def _full_summary(date="2026-05-26", **over):
    row = {"date": date, "pv_kwh": 18.4, "battery_charge_kwh": 11.8,
           "battery_discharge_kwh": 9.2, "peak_soc_pct": 100.0,
           "end_soc_pct": 62.0, "grid_import_kwh": 4.3, "grid_export_kwh": 6.8,
           "house_load_kwh": 30.0, "cheap_import_kwh": 3.1, "day_import_kwh": 0.9,
           "peak_import_kwh": 0.3, "import_cost_pence": 42.0,
           "export_earned_pence": 27.0, "standing_elec_pence": 55.0,
           "gas_kwh": 12.4, "gas_cost_pence": 88.0, "standing_gas_pence": 32.0,
           "self_sufficiency_pct": 86.0, "net_cost_pence": 190.0,
           "sunsynk_ok": 1, "octopus_ok": 1, "data_partial": 0}
    row.update(over)
    return row


def test_build_brief_full(tmp_db):
    db.upsert_daily_summary(_full_summary())
    msg = brief.build_daily_brief("2026-05-26", flex_rate_p=28.0)
    assert "PowerSOC — Energy Brief" in msg
    assert "Solar & Battery" in msg
    assert "18.4 kWh" in msg                  # generated
    assert "cheap 3.1 / day 0.9 / peak 0.3" in msg
    assert "Self-sufficiency: **86%**" in msg
    assert "Est. saving vs Flexible" in msg   # flex rate provided
    assert "Battery hit 100% SoC" in msg      # notable
    assert "Net exporter" in msg


def test_build_brief_degraded_sunsynk(tmp_db):
    db.upsert_daily_summary(_full_summary(sunsynk_ok=0))
    msg = brief.build_daily_brief("2026-05-26")
    assert "Sunsynk data unavailable" in msg
    # Electricity section still present.
    assert "Electricity (Cosy Octopus)" in msg
    # No saving line without a flex rate.
    assert "Est. saving vs Flexible" not in msg


def test_build_brief_degraded_octopus(tmp_db):
    db.upsert_daily_summary(_full_summary(octopus_ok=0))
    msg = brief.build_daily_brief("2026-05-26")
    assert "no data available" in msg


def test_build_brief_import_estimated_marker(tmp_db):
    # Octopus partial/unavailable but Sunsynk CT gave us an estimate -> the
    # section still renders with an "(est. from CT)" marker.
    db.upsert_daily_summary(_full_summary(octopus_ok=0, import_estimated=1,
                                          grid_import_kwh=8.07,
                                          cheap_import_kwh=7.8, day_import_kwh=0.2,
                                          peak_import_kwh=0.07,
                                          import_cost_pence=85.0))
    msg = brief.build_daily_brief("2026-05-26")
    assert "Electricity (Cosy Octopus)" in msg
    assert "(est. from CT)" in msg
    assert "8.1 kWh" in msg                            # estimated import
    assert "(cheap 7.8 / day 0.2 / peak 0.1)" in msg


def test_gas_section_shown_when_gas_present_even_without_elec(tmp_db):
    # octopus_ok tracks the electricity feed; a gas-only day (gas intervals
    # present, no electricity) must still render the Gas section.
    db.upsert_daily_summary(_full_summary(octopus_ok=0, gas_kwh=12.4))
    db.upsert_intervals("gas_intervals", [
        {"interval_start": "2026-05-26T00:00:00+01:00",
         "interval_end": "2026-05-26T00:30:00+01:00", "consumption_kwh": 1.2}])
    msg = brief.build_daily_brief("2026-05-26")
    assert "🔥 Gas" in msg and "12.4 kWh" in msg


def test_gas_section_hidden_without_gas_data(tmp_db):
    # No gas intervals -> no Gas section (don't imply a gas meter that isn't there).
    db.upsert_daily_summary(_full_summary())
    assert "🔥 Gas" not in brief.build_daily_brief("2026-05-26")


def test_today_outlook_section(tmp_db, monkeypatch):
    monkeypatch.setattr(weather, "get_forecast",
                        lambda: {"today": {"outlook": "high"}})
    monkeypatch.setattr(carbon, "get_intensity",
                        lambda: {"greenest": {"from": "2026-05-26T13:00Z", "g": 22}})
    db.upsert_daily_summary(_full_summary())
    msg = brief.build_daily_brief("2026-05-26")
    assert "🔆 Today" in msg
    assert "Solar outlook **high**" in msg
    # 13:00Z on a BST date renders as 14:00 local — the brief must show
    # Europe/London wall time, not UTC.
    assert "greenest grid ~14:00" in msg and "22 gCO₂" in msg


def test_saving_session_section(tmp_db, monkeypatch):
    from datetime import datetime, timedelta, timezone
    soon = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
    monkeypatch.setattr(octopus_graphql, "get_account_overview", lambda *a, **k: {
        "saving_sessions": {"next": {"start": soon, "points_per_kwh": 1800,
                                     "joined": False}}})
    db.upsert_daily_summary(_full_summary())
    msg = brief.build_daily_brief("2026-05-26")
    assert "Octoplus Saving Session" in msg
    assert "1800 pts/kWh" in msg
    assert "not joined" in msg


def test_saving_session_omitted_when_far_off(tmp_db, monkeypatch):
    from datetime import datetime, timedelta, timezone
    far = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()
    monkeypatch.setattr(octopus_graphql, "get_account_overview", lambda *a, **k: {
        "saving_sessions": {"next": {"start": far, "joined": True}}})
    db.upsert_daily_summary(_full_summary())
    assert "Saving Session" not in brief.build_daily_brief("2026-05-26")


def test_build_brief_partial_note(tmp_db):
    db.upsert_daily_summary(_full_summary(data_partial=1))
    # Seed 38 import intervals so the count shows in the note.
    rows = [{"interval_start": f"2026-05-26T00:{i:02d}:00", "interval_end": "x",
             "consumption_kwh": 0.1, "tariff_window": "day",
             "rate_p_per_kwh": 10.0, "cost_pence": 1.0} for i in range(38)]
    db.upsert_intervals("electricity_intervals", rows)
    msg = brief.build_daily_brief("2026-05-26")
    assert "partial (38/48 intervals" in msg
