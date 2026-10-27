"""Feature 14 — tariff simulator: this home's real half-hourly import priced
on Octopus's current alternatives (same usage, no load-shifting modelled)."""
from __future__ import annotations

import time
from datetime import date, timedelta

import pytest

import app as app_module
import auth
import config
import database as db
import octopus
import sync
import tariff_sim

TODAY = date(2026, 10, 22)


class _FakeOcto:
    def __init__(self, *a, **k):
        self.rate_calls = []

    def _get_paginated(self, path, params=None, max_pages=100):
        assert path == "/products/"
        return [
            {"code": "AGILE-24-10-01", "display_name": "Agile Octopus", "direction": "IMPORT"},
            {"code": "AGILE-OUTGOING-19-05-13", "display_name": "Agile Outgoing Octopus",
             "direction": "EXPORT"},
            {"code": "GO-VAR-22-10-14", "display_name": "Octopus Go", "direction": "IMPORT"},
            {"code": "VAR-22-11-01", "display_name": "Flexible Octopus", "direction": "IMPORT"},
            {"code": "OE-FIX-12M-26-10-01", "display_name": "Octopus 12M Fixed", "direction": "IMPORT"},
        ]

    def get_unit_rates(self, product, code, fuel, pf, pt):
        self.rate_calls.append(code)
        price = {"AGILE-24-10-01": 15.0, "GO-VAR-22-10-14": 20.0}.get(product, 30.0)
        return [{"value_inc_vat": price, "valid_from": "2020-01-01T00:00:00Z", "valid_to": None}]

    def get_standing_charge(self, product, code, fuel, at=None):
        return 50.0

    def close(self):
        pass


@pytest.fixture
def sim(monkeypatch):
    config.set_credential("OCTOPUS_API_KEY", "k")
    config.set_credential("OCTOPUS_ACCOUNT_NUMBER", "A-1")
    monkeypatch.setattr(tariff_sim, "_today", lambda: TODAY)
    fake = _FakeOcto()
    monkeypatch.setattr(tariff_sim.octopus, "OctopusClient", lambda *a, **k: fake)
    monkeypatch.setattr(sync, "resolve_meters", lambda *a, **k: octopus.MeterInfo(region="N"))
    for back in (1, 2):
        d = (TODAY - timedelta(days=back)).isoformat()
        db.upsert_intervals("electricity_intervals", [
            {"interval_start": f"{d}T{i // 2:02d}:{i % 2 * 30:02d}:00+0100", "interval_end": "x",
             "consumption_kwh": 0.5, "tariff_window": "day", "rate_p_per_kwh": 25.0,
             "cost_pence": 12.5} for i in range(48)])
        db.upsert_daily_summary({"date": d, "octopus_ok": 1, "standing_elec_pence": 60.0,
                                 "import_cost_pence": 600.0})
    return fake


def test_actual_usage_is_priced_on_each_alternative(sim):
    r = tariff_sim.run(days=30)
    rows = {x["name"]: x for x in r["tariffs"]}
    assert set(rows) == {"Agile Octopus", "Octopus Go", "Flexible Octopus"}   # imports only
    assert rows["Agile Octopus"]["import_p"] == pytest.approx(48 * 15.0)        # 48 kWh
    assert rows["Agile Octopus"]["standing_p"] == pytest.approx(2 * 50.0)
    assert r["actual"]["total_p"] == pytest.approx(1200.0 + 120.0)
    assert rows["Agile Octopus"]["delta_p"] == pytest.approx(820.0 - 1320.0)
    assert [x["name"] for x in r["tariffs"]][0] == "Agile Octopus"             # cheapest first
    assert r["kwh"] == pytest.approx(48.0) and r["days"] == 2
    assert all(c.endswith("-N") for c in sim.rate_calls)                      # the region


def test_results_are_stored_and_served(sim):
    application = app_module.create_app()
    c = application.test_client()
    uid = db.insert_user("admin", auth.hash_password("supersecret1"), role="admin")
    with c.session_transaction() as s:
        s["user_id"], s["epoch"] = uid, 0
    assert c.get("/api/tariff-sim").get_json()["data"]["result"] is None
    assert c.post("/api/tariff-sim/run", json={"days": 30}).get_json()["data"]["started"] is True
    for _ in range(100):
        if not c.get("/api/tariff-sim").get_json()["data"]["running"]:
            break
        time.sleep(0.02)
    res = c.get("/api/tariff-sim").get_json()["data"]["result"]
    assert res["tariffs"] and res["ran_at"]
    assert c.post("/api/tariff-sim/run", json={"days": 5000}).status_code == 400


def test_no_usage_means_nothing_to_compare(monkeypatch):
    config.set_credential("OCTOPUS_API_KEY", "k")
    config.set_credential("OCTOPUS_ACCOUNT_NUMBER", "A-1")
    monkeypatch.setattr(tariff_sim, "_today", lambda: TODAY)
    assert tariff_sim.run(days=30)["error"]
