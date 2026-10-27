"""Quick win 4 — the Data page: per-day source coverage, sync log, CT check
and an admin-only audit-log viewer."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

import app as app_module
import auth
import database as db
import insights
from routes import payloads

TODAY = date(2026, 10, 22)


@pytest.fixture
def client():
    application = app_module.create_app()
    application.config.update(TESTING=True)
    return application.test_client()


def _login(c, role="admin", name="admin"):
    uid = db.insert_user(name, auth.hash_password("supersecret1"), role=role)
    with c.session_transaction() as s:
        s["user_id"], s["epoch"] = uid, 0
    return c


def _rows(table, d, n, extra):
    db.upsert_intervals(table, [{"interval_start": f"{d}T{i // 2:02d}:{i % 2 * 30:02d}:00",
                                 "interval_end": "x", **extra} for i in range(n)])


def test_coverage_reports_each_source_per_day(monkeypatch):
    monkeypatch.setattr(payloads, "_today", lambda: TODAY)
    monkeypatch.setattr(payloads, "_coverage_sources", lambda: ["sunsynk", "elec", "gas"])
    d1, d2, d3 = ((TODAY - timedelta(days=b)).isoformat() for b in (1, 2, 3))
    _rows("solar_intervals", d1, 48, {"pv_kwh": 0.1})
    _rows("electricity_intervals", d1, 48, {"consumption_kwh": 0.1, "tariff_window": "day"})
    _rows("gas_intervals", d1, 48, {"consumption_kwh": 0.1})
    _rows("electricity_intervals", d2, 30, {"consumption_kwh": 0.1, "tariff_window": "day"})
    cov = payloads._coverage_data(days=3)
    by = {d["date"]: d for d in cov["days"]}
    assert by[d1]["status"] == "complete" and by[d1]["elec"] == 48
    assert by[d2]["status"] == "partial" and by[d2]["sunsynk"] == 0
    assert by[d3]["status"] == "missing"
    assert cov["sources"] == ["sunsynk", "elec", "gas"]


def test_coverage_route_and_page(client, monkeypatch):
    _login(client, role="user", name="viewer")
    r = client.get("/api/coverage?days=30")
    assert r.status_code == 200 and len(r.get_json()["data"]["days"]) == 30
    assert client.get("/api/coverage?days=abc").status_code == 400
    html = client.get("/data").get_data(as_text=True)
    assert 'id="coverage-grid"' in html and 'id="audit-card"' not in html   # non-admin


def test_audit_viewer_is_admin_only_and_filters(client):
    _login(client)
    with client.application.test_request_context():
        db.audit_add(1, "admin", "settings.system", details='{"keys": ["x"]}', ip_address="10.0.0.5")
        db.audit_add(None, "eve", "user.login_failed", ip_address="203.0.113.1")
    rows = client.get("/api/audit?action=user.login_failed").get_json()["data"]
    assert [r["action"] for r in rows] == ["user.login_failed"]
    assert "id=\"audit-card\"" in client.get("/data").get_data(as_text=True)
    v = client.application.test_client()
    _login(v, role="user", name="viewer")
    assert v.get("/api/audit").status_code == 403


def test_ct_check_route(client, monkeypatch):
    _login(client)
    monkeypatch.setattr(insights, "_today", lambda: TODAY)
    assert client.get("/api/insights/ct-check").get_json()["data"]["available"] is False


def test_coverage_infers_sources_from_data_when_none_are_configured(monkeypatch):
    monkeypatch.setattr(payloads, "_today", lambda: TODAY)
    d1 = (TODAY - timedelta(days=1)).isoformat()
    _rows("electricity_intervals", d1, 48, {"consumption_kwh": 0.1, "tariff_window": "day"})
    cov = payloads._coverage_data(days=2)
    assert cov["sources"] == ["elec"]
    assert {d["date"]: d["status"] for d in cov["days"]}[d1] == "complete"

