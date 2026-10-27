"""Tests for history aggregation (app._build_history_payload)."""
from __future__ import annotations

from datetime import date

import pytest

import app
import database as db


def _seed(dates, **over):
    for d in dates:
        row = {"date": d, "pv_kwh": 10.0, "grid_import_kwh": 4.0,
               "grid_export_kwh": 6.0, "gas_kwh": 12.0,
               "import_cost_pence": 40.0, "export_earned_pence": 20.0,
               "standing_elec_pence": 50.0, "gas_cost_pence": 70.0,
               "standing_gas_pence": 30.0, "net_cost_pence": 170.0}
        row.update(over)
        db.upsert_daily_summary(row)


def test_week_bounds_monday_to_sunday(tmp_db):
    # 2026-05-13 is a Wednesday -> week Mon 11 .. Sun 17.
    p = app._build_history_payload("week", date(2026, 5, 13))
    assert p["range"] == {"start": "2026-05-11", "end": "2026-05-17"}
    assert p["labels"] == ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    assert len(p["table"]) == 7


def test_month_bounds_and_costs(tmp_db):
    _seed([f"2026-04-{d:02d}" for d in range(1, 31)])
    p = app._build_history_payload("month", date(2026, 4, 15))
    assert p["range"] == {"start": "2026-04-01", "end": "2026-04-30"}
    assert len(p["labels"]) == 30
    # elec cost per day = 40 + 50 - 20 = 70p; 30 days = 2100p.
    assert p["totals"]["elec_cost"] == pytest.approx(2100.0)
    # gas per day = 70 + 30 = 100p; 30 days = 3000p.
    assert p["totals"]["gas_cost"] == pytest.approx(3000.0)


def test_year_groups_by_month(tmp_db):
    _seed(["2026-03-10", "2026-03-20", "2026-07-01"])
    p = app._build_history_payload("year", date(2026, 6, 1))
    assert p["labels"][0] == "Jan" and p["labels"][-1] == "Dec"
    assert len(p["table"]) == 12
    # March has 2 days, July 1, rest 0.
    march = p["cost"]["elec"][2]
    july = p["cost"]["elec"][6]
    assert march == pytest.approx(140.0)   # 2 * 70p
    assert july == pytest.approx(70.0)
    assert p["cost"]["elec"][0] is None     # January: no data, not "£0"


def test_history_saving_vs_flexible(tmp_db):
    # 5 days, each import 10 kWh @ actual cost 70p; Flexible would be 30p/kWh.
    _seed([f"2026-04-{d:02d}" for d in range(1, 6)],
          grid_import_kwh=10.0, import_cost_pence=70.0)
    db.upsert_tariff_rate("electricity_flexible", "2026-04-01", 30.0)
    p = app._build_history_payload("month", date(2026, 4, 15))
    # saving = 30p * 50 kWh - (5 * 70p) = 1500 - 350 = 1150p
    assert p["totals"]["flex_rate_p"] == 30.0
    assert p["totals"]["saving_pence"] == pytest.approx(1150.0)


def test_history_saving_none_without_flex_rate(tmp_db):
    _seed(["2026-04-01"], grid_import_kwh=5.0, import_cost_pence=40.0)
    p = app._build_history_payload("month", date(2026, 4, 1))
    assert p["totals"]["saving_pence"] is None


def test_empty_period_is_unknown_not_zero(tmp_db):
    """A period with no data is "–", not a free week (2026-10-07 rule)."""
    p = app._build_history_payload("week", date(2099, 1, 6))
    assert p["totals"]["elec_cost"] is None
    assert all(v is None for v in p["cost"]["elec"])
