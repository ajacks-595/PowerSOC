"""Regression tests for the 2026-10-26 review: tariff-agnostic surfaces and
the remaining P2/P3 items. Written to fail first: statement-period
reconciliation, non-Cosy tips/windows/tariff panel, Direct-Debit rate rows,
cached-forecast marking in the brief, and the host/cron intraday tick.
Hermetic (see conftest)."""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

import brief
import config
import database as db
import octopus
import sync
from routes import payloads

LONDON = ZoneInfo("Europe/London")
GO = "E-1R-GO-VAR-22-10-14-N"


def _go_periods():
    """Octopus Go on 2026-10-20 (BST): 00:00-00:30 27p, 00:30-05:30 7.5p,
    05:30-24:00 27p — stored as Octopus sends them (UTC)."""
    for vf, vt, p in (("2026-10-19T23:00:00Z", "2026-10-19T23:30:00Z", 27.0),
                      ("2026-10-19T23:30:00Z", "2026-10-20T04:30:00Z", 7.5),
                      ("2026-10-20T04:30:00Z", "2026-10-20T23:00:00Z", 27.0)):
        db.upsert_tariff_rate("electricity_import", vf, p, vt)


# ---------- reconciliation by statement period -----------------------------------

def test_reconcile_compares_each_bill_with_its_own_period(monkeypatch):
    import octopus_graphql
    monkeypatch.setattr(sync, "today_date", lambda: date(2026, 10, 26))
    monkeypatch.setattr(octopus_graphql, "get_latest_statements", lambda: [
        {"from": "2026-09-14", "to": "2026-10-13", "total_pence": 3000.0}])
    for d, net in (("2026-09-13", 999.0), ("2026-09-20", 1000.0),
                   ("2026-10-05", 1500.0), ("2026-10-20", 500.0)):
        db.upsert_daily_summary({"date": d, "net_cost_pence": net, "octopus_ok": 1})
    d = payloads._reconcile_data()
    st = d["statements"][0]
    assert st["our_cost_pence"] == pytest.approx(2500.0)     # 14 Sep - 13 Oct only
    assert st["billed_pence"] == 3000.0 and st["delta_pence"] == pytest.approx(-500.0)
    assert st["days_with_data"] == 2 and st["days"] == 30
    # Calendar months no longer pretend to have a bill keyed by end-month.
    assert all(m["billed_pence"] is None for m in d["months"])


# ---------- tariff-aware windows, tips and panel ------------------------------------

def test_current_window_follows_the_tariff_not_cosy_hours():
    config.set_text("import_tariff_code", GO)
    _go_periods()
    at_2am = datetime(2026, 10, 20, 2, 0, tzinfo=LONDON)
    at_5pm = datetime(2026, 10, 20, 17, 0, tzinfo=LONDON)
    assert payloads.current_window(at_2am) == "cheap"
    assert payloads.current_window(at_5pm) == "day"           # Go has no peak
    config.set_text("import_tariff_code", "E-1R-COSY-22-12-08-N")
    assert payloads.current_window(at_5pm) == "peak"           # Cosy: 16-19


def test_tips_name_the_tariffs_own_cheap_window():
    config.set_text("import_tariff_code", GO)
    _go_periods()
    tips = payloads._build_tips({}, datetime(2026, 10, 20, 12, 0, tzinfo=LONDON))
    text = " ".join(tips)
    assert "00:30–05:30" in text
    assert "04:00" not in text and "16:00–19:00" not in text


def test_band_times_from_stored_periods():
    _go_periods()
    times = payloads.band_times(date(2026, 10, 20), GO)
    assert times["cheap"] == "00:30–05:30"
    assert times["day"] == "00:00–00:30 · 05:30–24:00"
    assert "peak" not in times


# ---------- Direct Debit rows -----------------------------------------------------

def test_direct_debit_rows_win_over_non_direct_debit():
    rows = [{"value_inc_vat": 30.0, "valid_from": "2026-01-01T00:00:00Z", "valid_to": None,
             "payment_method": "NON_DIRECT_DEBIT"},
            {"value_inc_vat": 28.0, "valid_from": "2026-01-01T00:00:00Z", "valid_to": None,
             "payment_method": "DIRECT_DEBIT"}]
    assert [r["value_inc_vat"] for r in octopus.prefer_direct_debit(rows)] == [28.0]
    plain = [{"value_inc_vat": 25.0, "payment_method": None}]
    assert octopus.prefer_direct_debit(plain) == plain


def test_unit_rates_and_standing_charges_drop_non_dd_duplicates(monkeypatch):
    c = octopus.OctopusClient("k", "A-1")
    rows = [{"value_inc_vat": 30.0, "valid_from": "2026-01-01T00:00:00Z",
             "payment_method": "NON_DIRECT_DEBIT"},
            {"value_inc_vat": 28.0, "valid_from": "2026-01-01T00:00:00Z",
             "payment_method": "DIRECT_DEBIT"}]
    monkeypatch.setattr(c, "_get_paginated", lambda *a, **k: list(rows))
    assert [r["value_inc_vat"] for r in c.get_unit_rates("P", "T", "electricity", "a", "b")] == [28.0]
    assert c.get_standing_charge("P", "T", "electricity") == 28.0


# ---------- the brief marks cached outlook data ------------------------------------

def test_brief_marks_a_cached_forecast(monkeypatch):
    import weather
    monkeypatch.setattr(weather, "get_forecast", lambda: {
        "available": True, "stale": True, "today": {"outlook": "high"}})
    text = brief.build_daily_brief("2026-10-20")
    assert "Solar outlook **high** (cached)" in text


# ---------- host/cron installs get the intraday tick ------------------------------

def test_cron_tick_runs_the_intraday_jobs(monkeypatch):
    import sync_daily
    ran = []
    monkeypatch.setattr(sync, "intraday_tick", lambda: ran.append("tick"))
    monkeypatch.setattr(sync, "heartbeat", lambda ok=True: ran.append(("hb", ok)))
    assert sync_daily.main(["sync_daily.py", "--tick"]) == 0
    assert ran[0] == "tick"


def test_cron_file_schedules_the_tick():
    from pathlib import Path
    cron = (Path(__file__).resolve().parent.parent / "powersoc.cron").read_text()
    assert "sync_daily.py --tick" in cron
