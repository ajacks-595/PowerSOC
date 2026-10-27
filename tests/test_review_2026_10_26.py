"""Regression tests for the 2026-10-26 review: data correctness.

Each test was written to fail first. Covers: per-day tariff agreements
(backfill priced history on today's tariff), stale-cache fallback when meter
discovery blips, prices that must never be overwritten with NULL, unknown
prices that must never be costed at 0p, the next-day 00:00 consumption row,
missing data rendering as "–" (the 2026-10-07 rule, never implemented until
now), phantom summaries, per-source backfill gap filling and repricing.
Hermetic per the project rules (see conftest)."""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import app as app_module
import auth
import brief
import config
import database as db
import octopus
import sync
from octopus import MeterInfo, OctopusClient
from routes import payloads

UTC = timezone.utc

# The live account's import agreement history (2026-10-26), trimmed.
AGREEMENTS = [
    {"tariff_code": "E-1R-COSY-22-12-08-N",
     "valid_from": "2024-09-24T00:00:00+01:00", "valid_to": "2025-10-01T00:00:00+01:00"},
    {"tariff_code": "E-1R-COSY-FIX-12M-25-09-24-N",
     "valid_from": "2025-10-01T00:00:00+01:00", "valid_to": "2026-10-01T00:00:00+01:00"},
    {"tariff_code": "E-1R-COSY-22-12-08-N",
     "valid_from": "2026-10-01T00:00:00+01:00", "valid_to": None},
]
FIX = "E-1R-COSY-FIX-12M-25-09-24-N"
VAR = "E-1R-COSY-22-12-08-N"


def _settings(**over):
    base = dict(sunsynk_configured=False, octopus_configured=True,
                octopus_api_key="k", octopus_account_number="A-1",
                octopus_mpan="", octopus_meter_serial="",
                octopus_export_mpan="", octopus_export_serial="",
                octopus_mprn="", octopus_gas_serial="", gas_units="m3",
                mattermost_configured=False,
                sunsynk_username="", sunsynk_password="", sunsynk_plant_id="",
                sunsynk_inverter_sn="")
    base.update(over)
    return SimpleNamespace(**base)


def _rows(date, n=48, kwh=0.5, offset="+01:00"):
    out = []
    for i in range(n):
        h, m = divmod(i * 30, 60)
        out.append({"consumption": kwh,
                    "interval_start": f"{date}T{h:02d}:{m:02d}:00{offset}",
                    "interval_end": f"{date}T{h:02d}:{m:02d}:00{offset}"})
    return out


def _elec(date, n=48, rate=10.0, kwh=0.5, window="day"):
    out = []
    for i in range(n):
        h, m = divmod(i * 30, 60)
        ts = f"{date}T{h:02d}:{m:02d}:00+0100"
        out.append({"interval_start": ts, "interval_end": ts,
                    "consumption_kwh": kwh, "tariff_window": window,
                    "rate_p_per_kwh": rate,
                    "cost_pence": None if rate is None else kwh * rate})
    return out


def _solar(date, n=48, pv=0.2, load=0.4, imp=0.0, exp=0.0, soc=50.0):
    out = []
    for i in range(n):
        h, m = divmod(i * 30, 60)
        out.append({"interval_start": f"{date}T{h:02d}:{m:02d}:00",
                    "pv_kwh": pv, "house_load_kwh": load,
                    "grid_import_kwh": imp, "grid_export_kwh": exp,
                    "battery_charge_kwh": 0.0, "battery_discharge_kwh": 0.0,
                    "soc_pct": soc})
    return out


@pytest.fixture
def client():
    application = app_module.create_app()
    application.config.update(TESTING=True)
    return application.test_client()


@pytest.fixture
def admin(client):
    r = client.post("/setup", data={"setup_token": auth.setup_token(),
                                    "username": "admin", "password": "supersecret1"})
    assert r.status_code in (302, 303)
    return client


# ---------- Octopus: next-day boundary row ---------------------------------

def test_consumption_drops_the_next_days_midnight_row(monkeypatch):
    """Octopus answers period_to inclusively: a day's fetch also returned the
    NEXT day's 00:00 reading, priced against rates that end at midnight (so
    NULL) — and, backfilling newest-first, it overwrote that day's correctly
    priced row. 169 live days had exactly one unpriced 00:00 interval."""
    rows = _rows("2026-05-01") + _rows("2026-05-02", n=1)
    c = OctopusClient("k", "A-1")
    monkeypatch.setattr(c, "_get", lambda p, params=None: {"results": rows, "next": None})
    rates = [{"value_inc_vat": 10.0, "valid_from": "2026-04-30T23:00:00Z",
              "valid_to": "2026-05-01T23:00:00Z"}]
    for fetch in (lambda: c.fetch_electricity_import("M", "S", "2026-05-01", rates),
                  lambda: c.fetch_electricity_export("M", "S", "2026-05-01", rates),
                  lambda: c.fetch_gas("M", "S", "2026-05-01", rates)):
        day = fetch()
        assert day.count == 48
        assert all(iv["interval_start"].startswith("2026-05-01") for iv in day.intervals)
        assert all(iv["rate_p_per_kwh"] == 10.0 for iv in day.intervals)


# ---------- agreement history ------------------------------------------------

def _account(agreements=AGREEMENTS):
    return {"properties": [{
        "electricity_meter_points": [
            {"mpan": "IMP", "is_export": False, "meters": [{"serial_number": "S1"}],
             "agreements": agreements},
            {"mpan": "EXP", "is_export": True, "meters": [{"serial_number": "X1"}],
             "agreements": [{"tariff_code": "E-1R-OUTGOING-FIX-12M-19-05-13-N",
                             "valid_from": "2024-08-14T00:00:00+01:00",
                             "valid_to": "2025-08-14T00:00:00+01:00"},
                            {"tariff_code": "E-1R-OUTGOING-VAR-24-10-26-N",
                             "valid_from": "2025-08-14T00:00:00+01:00",
                             "valid_to": None}]}],
        "gas_meter_points": [
            {"mprn": "G", "meters": [{"serial_number": "G1"}],
             "agreements": [{"tariff_code": "G-1R-VAR-22-11-01-N",
                             "valid_from": "2024-06-15T00:00:00+01:00",
                             "valid_to": None}]}]}]}


def test_discovery_keeps_agreement_history(monkeypatch):
    c = OctopusClient("k", "A-1")
    monkeypatch.setattr(c, "_get", lambda p, params=None: _account())
    info = c.discover_meters(now=datetime(2026, 10, 26, tzinfo=UTC))
    assert info.import_tariff_code == VAR
    assert info.tariff_on("import", "2026-03-01") == FIX
    assert info.tariff_on("import", "2026-10-20") == VAR
    assert info.tariff_on("import", "2025-06-01") == VAR
    assert info.tariff_on("import", "2024-01-01") == ""      # before supply began
    assert info.tariff_on("export", "2025-01-01") == "E-1R-OUTGOING-FIX-12M-19-05-13-N"
    assert info.tariff_on("gas", "2025-01-01") == "G-1R-VAR-22-11-01-N"
    # Round-trips through the discovery cache (asdict -> json -> MeterInfo).
    again = MeterInfo(**json.loads(json.dumps(sync.asdict(info))))
    assert again.tariff_on("import", "2026-03-01") == FIX


def test_tariff_on_without_history_uses_the_current_code():
    """Explicit settings / an old cache carry no history: keep today's code."""
    info = MeterInfo(import_tariff_code=VAR)
    assert info.tariff_on("import", "2020-01-01") == VAR


class _FakeOcto:
    """Records which tariff each day's rates/standing charges were asked for."""
    def __init__(self, rate=10.0, fail_rates=False):
        self.rate_calls, self.standing_calls = [], []
        self.rate, self.fail_rates = rate, fail_rates

    day_bounds = staticmethod(OctopusClient.day_bounds)

    def get_unit_rates(self, product, tariff, fuel, pf, pt):
        self.rate_calls.append((tariff, pf[:10]))
        if self.fail_rates:
            raise octopus.OctopusConnectionError("network error: Timeout")
        return [{"value_inc_vat": self.rate, "valid_from": pf, "valid_to": pt}]

    def get_standing_charge(self, product, tariff, fuel, at=None):
        self.standing_calls.append((tariff, at.date().isoformat() if at else None))
        return 50.0 if "FIX" in tariff else 60.0

    def fetch_electricity_import(self, mpan, serial, date, rates, tariff_code=None):
        return _consumption_day(date, rates, tariff_code)

    def fetch_electricity_export(self, *a, **k):
        return octopus.ConsumptionDay(a[2], [], 0, True)

    def fetch_gas(self, *a, **k):
        return octopus.ConsumptionDay(a[2], [], 0, True)

    def close(self):
        pass


def _consumption_day(date, rates, tariff_code):
    out = []
    for iv in _elec(date, rate=None):
        r = octopus.rate_for(datetime.fromisoformat(iv["interval_start"]), rates) if rates else None
        p = r["value_inc_vat"] if r else None
        out.append({**iv, "rate_p_per_kwh": p,
                    "cost_pence": None if p is None else iv["consumption_kwh"] * p})
    return octopus.ConsumptionDay(date, out, len(out), False)


def test_each_day_is_priced_on_its_own_agreement():
    info = MeterInfo(import_mpan="IMP", import_serial="S1", import_tariff_code=VAR,
                     agreements={"import": AGREEMENTS})
    octo = _FakeOcto()
    sync.sync_octopus_day(octo, info, "2026-03-01")
    sync.sync_octopus_day(octo, info, "2026-10-20")
    assert [t for t, _ in octo.rate_calls] == [FIX, VAR]
    assert sync._standing_charges(octo, info, "2026-03-01") == (50.0, None)
    assert octo.standing_calls[-1][0] == FIX


def test_no_agreement_on_a_day_means_no_pricing_not_todays_price():
    info = MeterInfo(import_mpan="IMP", import_serial="S1", import_tariff_code=VAR,
                     agreements={"import": AGREEMENTS})
    octo = _FakeOcto()
    sync.sync_octopus_day(octo, info, "2024-01-01")
    assert octo.rate_calls == []
    assert sync._standing_charges(octo, info, "2024-01-01") == (None, None)


def test_standing_charge_for_a_past_date_never_falls_back_to_the_newest():
    c = OctopusClient("k", "A-1")
    c._standing_cache[("P", "T", "electricity")] = [
        {"value_inc_vat": 60.0, "valid_from": "2026-01-01T00:00:00Z", "valid_to": None}]
    assert c.get_standing_charge("P", "T", "electricity",
                                 datetime(2025, 1, 1, tzinfo=UTC)) is None
    assert c.get_standing_charge("P", "T", "electricity") == 60.0


# ---------- discovery blips ---------------------------------------------------

def test_failed_rediscovery_falls_back_to_the_stale_cache(caplog):
    info = MeterInfo(import_mpan="IMP", import_serial="S1", import_tariff_code=VAR)
    db.setting_set(sync._METER_CACHE_KEY, json.dumps(
        {"account": "A-1", "ts": time.time() - 3 * 86400, "info": sync.asdict(info)}))

    class _Down:
        def discover_meters(self):
            raise octopus.OctopusConnectionError("network error: Timeout")

    got = sync.resolve_meters(_settings(), _Down())
    assert got.import_tariff_code == VAR
    assert "cached discovery" in caplog.text


def test_run_daily_never_clears_the_stored_tariff_code(monkeypatch):
    config.set_text("import_tariff_code", VAR)
    monkeypatch.setattr(config.Settings, "load", staticmethod(lambda: _settings()))
    monkeypatch.setattr(sync.octopus, "OctopusClient", lambda *a, **k: _FakeOcto())
    monkeypatch.setattr(sync, "resolve_meters", lambda *a, **k: MeterInfo())
    sync.run_daily("2026-10-20")
    assert config.get_text("import_tariff_code") == VAR


# ---------- prices are never overwritten with NULL / never 0p ----------------

def test_a_rate_blip_keeps_the_stored_prices():
    db.upsert_intervals("electricity_intervals", _elec("2026-10-20", rate=24.8))
    info = MeterInfo(import_mpan="IMP", import_serial="S1", import_tariff_code=VAR)
    sync.sync_octopus_day(_FakeOcto(fail_rates=True), info, "2026-10-20")
    rows = db.intervals_for_date("electricity_intervals", "2026-10-20")
    assert len(rows) == 48
    assert all(r["rate_p_per_kwh"] == 24.8 for r in rows)
    assert sync.compute_daily_summary("2026-10-20")["import_cost_pence"] == pytest.approx(48 * 0.5 * 24.8)


def test_unpriced_import_is_an_unknown_cost_not_a_free_day():
    db.upsert_intervals("electricity_intervals", _elec("2026-10-20", rate=None))
    s = sync.compute_daily_summary("2026-10-20")
    assert s["grid_import_kwh"] == pytest.approx(24.0)
    assert s["import_cost_pence"] is None
    assert s["net_cost_pence"] is None
    assert sync.saving_vs_flexible_pence(25.0, 24.0, None) is None


def test_one_unpriced_interval_makes_the_cost_unknown():
    rows = _elec("2026-10-20", rate=20.0)
    rows[0].update(rate_p_per_kwh=None, cost_pence=None)
    db.upsert_intervals("electricity_intervals", rows)
    assert sync.compute_daily_summary("2026-10-20")["import_cost_pence"] is None


def test_unpriced_days_are_retried_by_catchup_and_backfill(monkeypatch):
    db.upsert_daily_summary({"date": "2026-01-10", "octopus_ok": 1, "sunsynk_ok": 0,
                             "data_partial": 0, "import_estimated": 0,
                             "import_cost_pence": None})
    from datetime import date
    assert sync._already_stored(date(2026, 1, 10), date(2026, 10, 26), False) is False


def test_ct_estimate_is_priced_from_stored_periods_on_a_non_cosy_tariff():
    """The CT estimate always used Cosy clock windows: on Go it invented peak
    kWh (falsely firing the high_peak alert) and mispriced by ~40%."""
    config.set_text("import_tariff_code", "E-1R-GO-VAR-22-10-14-N")
    for vf, vt, p in (("2026-10-19T23:00:00Z", "2026-10-19T23:30:00Z", 27.0),
                      ("2026-10-19T23:30:00Z", "2026-10-20T04:30:00Z", 7.5),
                      ("2026-10-20T04:30:00Z", "2026-10-20T23:00:00Z", 27.0)):
        db.upsert_tariff_rate("electricity_import", vf, p, vt)
    solar = _solar("2026-10-20")
    for iv in solar:
        iv["grid_import_kwh"] = 1.0 if iv["interval_start"][11:16] in ("02:00", "17:00") else 0.0
    db.upsert_intervals("solar_intervals", solar)
    s = sync.compute_daily_summary("2026-10-20")
    assert s["import_estimated"] == 1
    assert s["cheap_import_kwh"] == pytest.approx(1.0)
    assert s["peak_import_kwh"] == pytest.approx(0.0)       # Go has no peak
    assert s["day_import_kwh"] == pytest.approx(1.0)
    assert s["import_cost_pence"] == pytest.approx(34.5)


def test_ct_estimate_with_no_known_rate_is_unknown_not_zero():
    solar = _solar("2026-10-20", imp=0.5)
    db.upsert_intervals("solar_intervals", solar)
    s = sync.compute_daily_summary("2026-10-20")
    assert s["import_estimated"] == 1 and s["grid_import_kwh"] == pytest.approx(24.0)
    assert s["import_cost_pence"] is None and s["net_cost_pence"] is None


# ---------- missing data is never zero ---------------------------------------

def test_summary_without_any_source_is_all_unknown():
    s = sync.compute_daily_summary("2026-10-20")
    for k in ("pv_kwh", "grid_import_kwh", "grid_export_kwh", "house_load_kwh",
              "import_cost_pence", "net_cost_pence", "gas_kwh"):
        assert s[k] is None, k


def test_run_daily_writes_nothing_when_no_source_returned_data(monkeypatch):
    monkeypatch.setattr(config.Settings, "load", staticmethod(lambda: _settings()))
    monkeypatch.setattr(sync.octopus, "OctopusClient", lambda *a, **k: _FakeOcto())
    monkeypatch.setattr(sync, "resolve_meters", lambda *a, **k: MeterInfo())
    res = sync.run_daily("2026-10-20")
    assert db.get_daily_summary("2026-10-20") is None
    assert res.get("no_data") is True


def test_run_daily_keeps_a_good_summary_when_a_rerun_gets_nothing(monkeypatch):
    db.upsert_intervals("electricity_intervals", _elec("2026-10-20"))
    db.upsert_daily_summary(sync.compute_daily_summary("2026-10-20"))
    monkeypatch.setattr(config.Settings, "load", staticmethod(lambda: _settings()))
    monkeypatch.setattr(sync.octopus, "OctopusClient", lambda *a, **k: _FakeOcto())
    monkeypatch.setattr(sync, "resolve_meters", lambda *a, **k: MeterInfo())
    sync.run_daily("2026-10-20")
    assert db.get_daily_summary("2026-10-20")["grid_import_kwh"] == pytest.approx(24.0)


def test_brief_renders_unknown_costs_as_dashes():
    text = brief.build_daily_brief("2026-10-20")
    assert "£0.00" not in text
    assert "Net electricity cost: **–**" in text
    db.upsert_intervals("electricity_intervals", _elec("2026-10-21", rate=None))
    text = brief.build_daily_brief("2026-10-21", flex_rate_p=25.0)
    assert "saving vs Flexible" not in text
    assert "Import cost: **–**" in text


def test_widget_renders_missing_values_as_dashes(client):
    html = client.get("/widget/today").get_data(as_text=True)
    assert "0.0 kWh" not in html and "£0.00" not in html
    assert html.count("–") >= 5


def test_day_payload_states_how_firm_the_cost_is():
    db.upsert_intervals("electricity_intervals", _elec("2026-10-20"))
    db.upsert_daily_summary(sync.compute_daily_summary("2026-10-20"))
    assert payloads._build_day_payload("2026-10-20")["cost_status"] == "confirmed"
    db.upsert_intervals("electricity_intervals", _elec("2026-10-21", n=30))
    db.upsert_daily_summary(sync.compute_daily_summary("2026-10-21"))
    assert payloads._build_day_payload("2026-10-21")["cost_status"] == "provisional"
    db.upsert_intervals("solar_intervals", _solar("2026-10-22", imp=0.5))
    db.upsert_daily_summary(sync.compute_daily_summary("2026-10-22"))
    assert payloads._build_day_payload("2026-10-22")["cost_status"] == "unpriced"
    assert payloads._build_day_payload("2026-10-23")["cost_status"] == "no_data"


def test_history_gaps_and_future_days_are_null_not_zero(monkeypatch):
    monkeypatch.setattr(sync, "today", lambda: "2026-10-21")
    db.upsert_intervals("electricity_intervals", _elec("2026-10-20"))
    db.upsert_daily_summary(sync.compute_daily_summary("2026-10-20"))
    from datetime import date
    p = payloads._build_history_payload("week", date(2026, 10, 20))   # Mon 19th..Sun 25th
    assert p["cost"]["elec"][0] is None        # 19th: no row (gap)
    assert p["cost"]["elec"][1] == pytest.approx(240.0)
    assert p["cost"]["elec"][6] is None        # 25th: future
    assert p["table"][0]["net_cost"] is None
    assert p["totals"]["elec_cost"] == pytest.approx(240.0)
    y = payloads._build_history_payload("year", date(2026, 10, 20))
    assert y["cost"]["elec"][0] is None        # January: no rows at all


def test_reconcile_month_without_data_is_unknown(monkeypatch):
    import octopus_graphql
    monkeypatch.setattr(octopus_graphql, "get_latest_statements", lambda: [])
    d = payloads._reconcile_data(months=2)
    assert all(m["our_cost_pence"] is None for m in d["months"])


def test_avg7_and_bill_forecast_ignore_days_without_data(monkeypatch):
    t = sync.today_date()
    from datetime import timedelta
    good = (t - timedelta(days=1)).isoformat()
    db.upsert_intervals("electricity_intervals", _elec(good))
    db.upsert_daily_summary(sync.compute_daily_summary(good))
    # A legacy all-zero phantom row (written by older versions).
    db.upsert_daily_summary({"date": (t - timedelta(days=2)).isoformat(),
                             "sunsynk_ok": 0, "octopus_ok": 0, "pv_kwh": 0.0,
                             "grid_import_kwh": 0.0, "net_cost_pence": 0.0})
    a = payloads._avg7()
    assert a["grid_import_kwh"] == pytest.approx(24.0)


def test_phantom_summaries_are_pruned():
    db.upsert_daily_summary({"date": "2024-06-01", "sunsynk_ok": 0, "octopus_ok": 0,
                             "standing_elec_pence": 56.6, "net_cost_pence": 85.6})
    db.upsert_intervals("export_intervals", [{
        "interval_start": "2024-09-01T12:00:00+0100", "interval_end": "x",
        "export_kwh": 1.0, "rate_p_per_kwh": 15.0, "earned_pence": 15.0}])
    db.upsert_daily_summary({"date": "2024-09-01", "sunsynk_ok": 0, "octopus_ok": 0})
    assert db.prune_phantom_summaries() == 1
    assert db.get_daily_summary("2024-06-01") is None
    assert db.get_daily_summary("2024-09-01") is not None


# ---------- price banding -------------------------------------------------------

@pytest.mark.parametrize("rates,rate,band", [
    ([27.0, 7.5, 27.0], 7.5, "cheap"),          # Go: 3 periods, 2 levels
    ([27.0, 7.5, 27.0], 27.0, "day"),
    ([25.0, 15.0, 25.0, 35.0, 25.0], 15.0, "cheap"),    # Flux
    ([25.0, 15.0, 25.0, 35.0, 25.0], 35.0, "peak"),
    ([25.0, 15.0, 25.0, 35.0, 25.0], 25.0, "day"),
    ([24.5] * 3, 24.5, "day"),                  # flat
])
def test_price_band_uses_price_levels_not_period_counts(rates, rate, band):
    assert octopus._price_band(rate, rates) == band


# ---------- backfill: per-source gaps, no phantoms, repricing -----------------

def test_backfill_writes_no_summaries_for_days_with_no_data(monkeypatch):
    monkeypatch.setattr(config.Settings, "load", staticmethod(lambda: _settings()))
    monkeypatch.setattr(sync.octopus, "OctopusClient", lambda *a, **k: _FakeOcto())
    monkeypatch.setattr(sync, "resolve_meters", lambda *a, **k: MeterInfo(
        import_mpan="IMP", import_serial="S1", import_tariff_code=VAR,
        agreements={"import": AGREEMENTS}))
    monkeypatch.setattr(sync.time, "sleep", lambda s: None)
    monkeypatch.setattr(sync, "_today_local", lambda: datetime(2024, 9, 26).date())
    sync.run_backfill(max_days=5)
    # 2024-09-21..23 predate the first agreement: nothing fetched, nothing written.
    assert db.get_daily_summary("2024-09-22") is None
    assert db.get_daily_summary("2024-09-25") is not None


def test_backfill_only_fetches_the_missing_source(monkeypatch):
    for d in ("2026-10-01", "2026-10-02"):
        db.upsert_intervals("electricity_intervals", _elec(d))
        db.upsert_daily_summary(sync.compute_daily_summary(d))
    info = MeterInfo(import_mpan="IMP", import_serial="S1", import_tariff_code=VAR,
                     mprn="G", gas_serial="G1", gas_tariff_code="G-1R-VAR-22-11-01-N")
    calls = []

    def fake_day(octo, meters, date, gas_units="m3", only=None):
        calls.append((date, tuple(sorted(only or ()))))
        return {}
    monkeypatch.setattr(config.Settings, "load", staticmethod(lambda: _settings()))
    monkeypatch.setattr(sync.octopus, "OctopusClient", lambda *a, **k: _FakeOcto())
    monkeypatch.setattr(sync, "resolve_meters", lambda *a, **k: info)
    monkeypatch.setattr(sync, "sync_octopus_day", fake_day)
    monkeypatch.setattr(sync, "_standing_charges", lambda *a, **k: (None, None))
    monkeypatch.setattr(sync.time, "sleep", lambda s: None)
    monkeypatch.setattr(sync, "_today_local", lambda: datetime(2026, 10, 26).date())
    sync.run_backfill(start="2026-10-01", max_days=3)
    assert ("2026-10-01", ("octopus_gas",)) in calls
    assert ("2026-10-02", ("octopus_gas",)) in calls


def test_reprice_history_fixes_rates_from_the_agreement_of_each_day(monkeypatch):
    db.upsert_intervals("electricity_intervals", _elec("2026-03-01", rate=99.0))
    rows = _elec("2026-10-20", rate=24.8)
    rows[0].update(rate_p_per_kwh=None, cost_pence=None)     # the midnight NULL
    db.upsert_intervals("electricity_intervals", rows)
    db.upsert_intervals("export_intervals", [{
        "interval_start": "2026-03-01T12:00:00+0000", "interval_end": "x",
        "export_kwh": 2.0, "rate_p_per_kwh": None, "earned_pence": None}])
    info = MeterInfo(import_mpan="IMP", import_serial="S1", import_tariff_code=VAR,
                     export_mpan="EXP", export_serial="X1",
                     export_tariff_code="E-1R-OUTGOING-VAR-24-10-26-N",
                     agreements={"import": AGREEMENTS,
                                 "export": [{"tariff_code": "E-1R-OUTGOING-VAR-24-10-26-N",
                                             "valid_from": "2025-08-14T00:00:00+01:00",
                                             "valid_to": None}]})

    class _Rates(_FakeOcto):
        def get_unit_rates(self, product, tariff, fuel, pf, pt):
            self.rate_calls.append((tariff, pf[:10]))
            p = {FIX: 27.9, VAR: 24.85}.get(tariff, 12.0)
            return [{"value_inc_vat": p, "valid_from": "2020-01-01T00:00:00Z", "valid_to": None}]
    octo = _Rates()
    monkeypatch.setattr(config.Settings, "load", staticmethod(lambda: _settings()))
    monkeypatch.setattr(sync.octopus, "OctopusClient", lambda *a, **k: octo)
    monkeypatch.setattr(sync, "resolve_meters", lambda *a, **k: info)
    out = sync.reprice_history()
    assert out["intervals"] == 97
    assert {r["rate_p_per_kwh"] for r in db.intervals_for_date("electricity_intervals", "2026-03-01")} == {27.9}
    assert {r["rate_p_per_kwh"] for r in db.intervals_for_date("electricity_intervals", "2026-10-20")} == {24.85}
    assert db.intervals_for_date("export_intervals", "2026-03-01")[0]["earned_pence"] == pytest.approx(24.0)
    s = db.get_daily_summary("2026-03-01")
    assert s["import_cost_pence"] == pytest.approx(24 * 27.9)
    assert s["standing_elec_pence"] == 50.0                   # FIX standing charge
    # One rates call per agreement window, not per day.
    assert len([c for c in octo.rate_calls if c[0] in (FIX, VAR)]) == 2


def test_flexible_rate_for_a_past_day_is_that_days_rate():
    c = OctopusClient("k", "A-1")
    seen = []

    def fake_paginated(path, params=None, max_pages=100):
        seen.append((path, dict(params or {})))
        if path == "/products/":
            return [{"display_name": "Flexible Octopus", "code": "VAR-22-11-01",
                     "direction": "IMPORT"}]
        return [{"value_inc_vat": 24.0, "valid_from": "2025-01-01T00:00:00Z",
                 "valid_to": None}]
    c._get_paginated = fake_paginated
    at = datetime(2025, 3, 1, 12, tzinfo=UTC)
    assert c.get_flexible_import_rate("N", at) == 24.0
    assert seen[0][1].get("available_at", "").startswith("2025-03-01")


def test_reprice_route_starts_a_job_and_reports_it(admin, monkeypatch):
    monkeypatch.setattr(sync, "reprice_history", lambda *a, **k: {
        "intervals": 12, "days": 1, "unpriced": 0, "error": None})
    r = admin.post("/api/backfill/reprice", json={})
    assert r.status_code == 200 and r.get_json()["data"]["started"] is True
    for _ in range(100):
        if not sync.backfill_status()["running"]:
            break
        time.sleep(0.02)
    st = admin.get("/api/backfill/status").get_json()["data"]
    assert st["running"] is False and st["mode"] == "reprice"
    assert st["repriced"]["intervals"] == 12


def test_reprice_route_is_admin_only(client):
    db.insert_user("viewer", auth.hash_password("supersecret1"))
    client.post("/auth/login", data={"username": "viewer", "password": "supersecret1"})
    assert client.post("/api/backfill/reprice", json={}).status_code == 403
