"""Quick win 6 — History: energy + self-sufficiency per bucket, the same
period last year, and a per-day calendar for the heatmap."""
from __future__ import annotations

from datetime import date

import pytest

import app as app_module
import auth
import database as db
from routes import payloads


def _day(d, net, pv=10.0, imp=4.0, load=8.0):
    db.upsert_daily_summary({"date": d, "octopus_ok": 1, "sunsynk_ok": 1,
                             "net_cost_pence": net, "import_cost_pence": net,
                             "pv_kwh": pv, "grid_import_kwh": imp, "house_load_kwh": load,
                             "grid_export_kwh": 1.0})


def test_buckets_carry_self_sufficiency():
    _day("2026-10-20", 100.0, imp=2.0, load=8.0)
    p = payloads._build_history_payload("week", date(2026, 10, 20))
    assert p["self_sufficiency"][1] == pytest.approx(75.0)
    assert p["self_sufficiency"][0] is None


def test_same_period_last_year_is_included():
    _day("2026-10-20", 100.0)
    _day("2025-10-21", 150.0)                       # the same weekday slot a year ago
    p = payloads._build_history_payload("week", date(2026, 10, 20))
    c = p["compare"]
    assert c["range"]["start"] == "2025-10-20" and c["totals"]["net_cost"] == pytest.approx(150.0)
    assert len(c["cost"]["net"]) == len(p["labels"])
    m = payloads._build_history_payload("month", date(2024, 2, 15))   # leap February
    assert m["compare"]["range"] == {"start": "2023-02-01", "end": "2023-02-28"}
    assert len(m["compare"]["cost"]["net"]) == len(m["labels"])       # padded to 29


def test_daily_calendar_for_a_year():
    _day("2026-03-01", 120.0)
    rows = payloads._calendar_data(2026)
    assert len(rows) == 365
    mar1 = next(r for r in rows if r["date"] == "2026-03-01")
    assert mar1["net_cost"] == 120.0 and mar1["self_sufficiency"] == pytest.approx(50.0)
    assert next(r for r in rows if r["date"] == "2026-03-02")["net_cost"] is None


def test_calendar_route_validates_the_year():
    application = app_module.create_app()
    c = application.test_client()
    uid = db.insert_user("u", auth.hash_password("supersecret1"))
    with c.session_transaction() as s:
        s["user_id"], s["epoch"] = uid, 0
    assert c.get("/api/history/calendar?year=2026").status_code == 200
    assert c.get("/api/history/calendar?year=abc").status_code == 400
    assert c.get("/api/history/calendar?year=1800").status_code == 400
