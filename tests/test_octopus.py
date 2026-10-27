"""Tests for the Octopus client. All HTTP is mocked — no live calls."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

import octopus
from octopus import (
    ConsumptionDay,
    MeterInfo,
    OctopusClient,
    cosy_window,
    m3_to_kwh,
    parse_tariff_code,
    rate_for,
    select_active_agreement,
    to_london,
)

UTC = timezone.utc


def _local(h, m=0):
    return datetime(2026, 5, 1, h, m)


# ---------- cosy_window ----------------------------------------------------

@pytest.mark.parametrize("h,m,expected", [
    (0, 0, "day"), (3, 59, "day"),
    (4, 0, "cheap"), (6, 30, "cheap"), (6, 59, "cheap"),
    (7, 0, "day"), (12, 59, "day"),
    (13, 0, "cheap"), (15, 59, "cheap"),
    (16, 0, "peak"), (18, 59, "peak"),
    (19, 0, "day"), (21, 59, "day"),
    (22, 0, "cheap"), (23, 59, "cheap"),
])
def test_cosy_window_boundaries(h, m, expected):
    assert cosy_window(_local(h, m)) == expected


def test_cosy_window_uses_local_time_across_dst():
    # June is BST (+1). 05:30 UTC -> 06:30 local -> cheap window.
    summer = to_london("2026-06-01T05:30:00Z")
    assert summer.hour == 6 and summer.minute == 30
    assert cosy_window(summer) == "cheap"
    # 15:30 UTC -> 16:30 BST -> peak.
    assert cosy_window(to_london("2026-06-01T15:30:00Z")) == "peak"
    # January is GMT. 05:30 UTC -> 05:30 local -> cheap.
    winter = to_london("2026-01-01T05:30:00Z")
    assert winter.hour == 5
    assert cosy_window(winter) == "cheap"
    # GMT 16:30 -> peak (no offset in winter).
    assert cosy_window(to_london("2026-01-01T16:30:00Z")) == "peak"


# ---------- gas conversion -------------------------------------------------

def test_m3_to_kwh_formula():
    assert m3_to_kwh(1.0) == pytest.approx(1.02264 * 39.5 / 3.6)
    assert m3_to_kwh(1.0) == pytest.approx(11.2206, abs=1e-3)
    assert m3_to_kwh(0.0) == 0.0


# ---------- tariff code parsing -------------------------------------------

def test_parse_tariff_code():
    assert parse_tariff_code("E-1R-COSY-22-12-08-A") == ("COSY-22-12-08", "A")
    assert parse_tariff_code("G-1R-OE-GAS-FIX-12M-24-04-11-C") \
        == ("OE-GAS-FIX-12M-24-04-11", "C")
    assert parse_tariff_code("E-1R-OUTGOING-FIX-12M-19-05-13-J") \
        == ("OUTGOING-FIX-12M-19-05-13", "J")


def test_parse_tariff_code_invalid():
    with pytest.raises(octopus.OctopusError):
        parse_tariff_code("")
    with pytest.raises(octopus.OctopusError):
        parse_tariff_code("E-1R")


# ---------- active agreement selection ------------------------------------

def _agreements():
    return [
        {"tariff_code": "OLD", "valid_from": "2022-01-01T00:00:00Z",
         "valid_to": "2024-01-01T00:00:00Z"},
        {"tariff_code": "CURRENT", "valid_from": "2024-01-01T00:00:00Z",
         "valid_to": "2027-01-01T00:00:00Z"},
        {"tariff_code": "FUTURE", "valid_from": "2027-01-01T00:00:00Z",
         "valid_to": None},
    ]


def test_select_active_agreement_picks_current_not_latest():
    now = datetime(2026, 5, 25, tzinfo=UTC)
    a = select_active_agreement(_agreements(), now)
    assert a["tariff_code"] == "CURRENT"   # NOT "FUTURE" (the latest by date)


def test_select_active_agreement_open_ended():
    a = select_active_agreement(
        [{"tariff_code": "LIVE", "valid_from": "2024-01-01T00:00:00Z",
          "valid_to": None}], datetime(2026, 5, 25, tzinfo=UTC))
    assert a["tariff_code"] == "LIVE"


def test_select_active_agreement_none_when_only_future():
    now = datetime(2026, 5, 25, tzinfo=UTC)
    fut = [{"tariff_code": "FUTURE", "valid_from": "2027-01-01T00:00:00Z",
            "valid_to": None}]
    assert select_active_agreement(fut, now) is None


def test_select_active_agreement_none_when_expired():
    now = datetime(2026, 5, 25, tzinfo=UTC)
    old = [{"tariff_code": "OLD", "valid_from": "2022-01-01T00:00:00Z",
            "valid_to": "2023-01-01T00:00:00Z"}]
    assert select_active_agreement(old, now) is None


# ---------- rate_for -------------------------------------------------------

def test_rate_for_matches_period():
    rates = [
        {"value_inc_vat": 12.0, "valid_from": "2026-05-01T00:00:00Z",
         "valid_to": "2026-05-01T03:00:00Z"},
        {"value_inc_vat": 7.5, "valid_from": "2026-05-01T03:00:00Z",
         "valid_to": "2026-05-01T06:00:00Z"},
    ]
    assert rate_for("2026-05-01T04:30:00Z", rates)["value_inc_vat"] == 7.5
    assert rate_for("2026-05-01T01:00:00Z", rates)["value_inc_vat"] == 12.0
    assert rate_for("2026-05-01T09:00:00Z", rates) is None


# ---------- tariff window classification -----------------------------------

def test_classify_window_cosy_is_time_based():
    cosy = "E-1R-COSY-22-12-08-A"
    assert octopus.classify_window(_local(5), tariff_code=cosy) == "cheap"
    assert octopus.classify_window(_local(17), tariff_code=cosy) == "peak"
    assert octopus.classify_window(_local(10), tariff_code=cosy) == "day"
    # No / unknown tariff code defaults to the time bands (legacy behaviour),
    # ignoring any rate passed in.
    assert octopus.classify_window(_local(5)) == "cheap"
    assert octopus.classify_window(_local(17), rate=1.0, day_rates=[1, 2, 3]) == "peak"


def test_classify_window_non_cosy_is_price_based():
    day = [5.0, 8.0, 10.0, 12.0, 20.0, 30.0]
    code = "E-1R-AGILE-24-10-01-A"
    # Cheapest band -> cheap even at a cosy 'peak' time (17:00).
    assert octopus.classify_window(_local(17), rate=5.0, day_rates=day, tariff_code=code) == "cheap"
    # Dearest band -> peak even at a cosy 'cheap' time (05:00).
    assert octopus.classify_window(_local(5), rate=30.0, day_rates=day, tariff_code=code) == "peak"
    # Banded by position in the day's price RANGE (5..30p): 11p is in the
    # bottom third, 15p in the middle (2026-10-26: terciles of the period
    # list banded Go entirely 'day').
    assert octopus.classify_window(_local(5), rate=11.0, day_rates=day, tariff_code=code) == "cheap"
    assert octopus.classify_window(_local(5), rate=15.0, day_rates=day, tariff_code=code) == "day"


def test_classify_window_flat_tariff_all_day():
    day = [25.0] * 8
    assert octopus.classify_window(_local(5), rate=25.0, day_rates=day,
                                   tariff_code="E-1R-FLAT-A") == "day"


# ---------- client with stubbed transport ---------------------------------

def _client(monkeypatch, responses):
    """Build an OctopusClient whose _get returns canned bodies by path
    substring. `responses` maps url-substring -> body dict."""
    c = OctopusClient(api_key="sk_test", account_number="A-AAAA1111")

    def fake_get(path, params=None):
        for frag, body in responses.items():
            if frag in path:
                return body
        return {"results": [], "next": None}

    monkeypatch.setattr(c, "_get", fake_get)
    return c


def _account_body():
    return {"properties": [{
        "electricity_meter_points": [
            {"mpan": "1111111111111", "is_export": False,
             "meters": [{"serial_number": "IMPORT-SER-1"}],
             "agreements": [
                 {"tariff_code": "E-1R-COSY-22-12-08-A",
                  "valid_from": "2024-01-01T00:00:00Z", "valid_to": None}]},
            {"mpan": "2222222222222", "is_export": True,
             "meters": [{"serial_number": "EXPORT-SER-9"}],
             "agreements": [
                 {"tariff_code": "E-1R-OUTGOING-FIX-12M-19-05-13-A",
                  "valid_from": "2024-01-01T00:00:00Z", "valid_to": None}]},
        ],
        "gas_meter_points": [
            {"mprn": "9999999999",
             "meters": [{"serial_number": "GAS-SER-7"}],
             "agreements": [
                 {"tariff_code": "G-1R-OE-GAS-FIX-12M-24-04-11-A",
                  "valid_from": "2024-01-01T00:00:00Z", "valid_to": None}]},
        ],
    }]}


def test_discover_meters_splits_import_export_gas(monkeypatch):
    c = _client(monkeypatch, {"/accounts/": _account_body()})
    info = c.discover_meters(now=datetime(2026, 5, 25, tzinfo=UTC))
    assert isinstance(info, MeterInfo)
    assert info.import_mpan == "1111111111111"
    assert info.import_serial == "IMPORT-SER-1"
    assert info.export_mpan == "2222222222222"
    # Export meter has its OWN serial — must not equal the import serial.
    assert info.export_serial == "EXPORT-SER-9"
    assert info.export_serial != info.import_serial
    assert info.mprn == "9999999999"
    assert info.gas_serial == "GAS-SER-7"
    assert info.region == "A"
    assert info.import_tariff_code == "E-1R-COSY-22-12-08-A"


def _consumption(n):
    """n half-hourly import rows starting at local midnight on 2026-05-01."""
    rows = []
    for i in range(n):
        h, m = divmod(i * 30, 60)
        rows.append({
            "consumption": 0.1,
            "interval_start": f"2026-05-01T{h:02d}:{m:02d}:00+01:00",
            "interval_end": f"2026-05-01T{h:02d}:{(m + 30) % 60:02d}:00+01:00",
        })
    return rows


def test_discover_picks_active_meter_not_first(monkeypatch):
    # LIVE is listed FIRST and the dead meter last: newest-first probing must
    # reject the dead one (no recent consumption) and land on LIVE — i.e. the
    # choice is driven by the data probe, not by list position.
    acct = {"properties": [{
        "electricity_meter_points": [
            {"mpan": "IMP", "is_export": False,
             "meters": [{"serial_number": "LIVE"}, {"serial_number": "OLD-DEAD"}],
             "agreements": [{"tariff_code": "E-1R-COSY-22-12-08-A",
                             "valid_from": "2024-01-01T00:00:00Z", "valid_to": None}]},
        ],
        "gas_meter_points": [],
    }]}
    c = OctopusClient(api_key="k", account_number="A-1")
    probes = []

    def fake_get(path, params=None):
        if "/accounts/" in path:
            return acct
        probes.append(path)
        live = "/LIVE/" in path
        return {"count": 1 if live else 0,
                "results": [{"consumption": 0.1}] if live else [], "next": None}
    monkeypatch.setattr(c, "_get", fake_get)
    info = c.discover_meters(now=datetime(2026, 5, 25, tzinfo=UTC))
    assert info.import_serial == "LIVE"          # NOT the dead meter
    assert info.region == "A"
    # One request per serial probed (was 3 dates × serials).
    assert len(probes) == 2


def test_discover_falls_back_to_last_serial_when_no_data(monkeypatch):
    acct = {"properties": [{
        "electricity_meter_points": [
            {"mpan": "IMP", "is_export": False,
             "meters": [{"serial_number": "A1"}, {"serial_number": "B2"}],
             "agreements": []},
        ], "gas_meter_points": []}]}
    c = OctopusClient(api_key="k", account_number="A-1")
    monkeypatch.setattr(c, "_get", lambda path, params=None:
                        acct if "/accounts/" in path else {"results": [], "next": None})
    monkeypatch.setattr(c, "fetch_electricity_import", lambda mpan, serial, d, rates=None:
                        ConsumptionDay(d, [], 0, True))   # no meter has data
    info = c.discover_meters(now=datetime(2026, 5, 25, tzinfo=UTC))
    assert info.import_serial == "B2"            # fallback: last serial


def test_fetch_import_attaches_window_rate_cost(monkeypatch):
    rates = [{"value_inc_vat": 10.0, "valid_from": "2026-05-01T00:00:00+01:00",
              "valid_to": "2026-05-02T00:00:00+01:00"}]
    c = _client(monkeypatch, {"consumption/": {"results": _consumption(48),
                                               "next": None}})
    day = c.fetch_electricity_import("1111111111111", "IMPORT-SER-1",
                                     "2026-05-01", rates=rates)
    assert isinstance(day, ConsumptionDay)
    assert day.count == 48
    assert day.partial is False
    first = day.intervals[0]
    assert first["consumption_kwh"] == 0.1
    assert first["rate_p_per_kwh"] == 10.0
    assert first["cost_pence"] == pytest.approx(1.0)
    assert first["tariff_window"] in ("cheap", "day", "peak")


def test_fetch_import_flags_partial(monkeypatch):
    c = _client(monkeypatch, {"consumption/": {"results": _consumption(38),
                                               "next": None}})
    day = c.fetch_electricity_import("1111111111111", "IMPORT-SER-1",
                                     "2026-05-01")
    assert day.count == 38
    assert day.partial is True


def test_fetch_import_window_is_tariff_aware(monkeypatch):
    rates = [
        {"value_inc_vat": 5.0,  "valid_from": "2026-05-01T00:00:00+01:00",
         "valid_to": "2026-05-01T08:00:00+01:00"},
        {"value_inc_vat": 15.0, "valid_from": "2026-05-01T08:00:00+01:00",
         "valid_to": "2026-05-01T16:00:00+01:00"},
        {"value_inc_vat": 30.0, "valid_from": "2026-05-01T16:00:00+01:00",
         "valid_to": "2026-05-02T00:00:00+01:00"},
    ]
    c = _client(monkeypatch, {"consumption/": {"results": _consumption(48), "next": None}})

    def at_10(day, field):
        return next(iv[field] for iv in day.intervals
                    if iv["interval_start"].startswith("2026-05-01T10:00"))

    cosy = c.fetch_electricity_import("M", "S", "2026-05-01", rates, "E-1R-COSY-22-12-08-A")
    agile = c.fetch_electricity_import("M", "S", "2026-05-01", rates, "E-1R-AGILE-24-10-01-A")
    def at(day, hhmm):
        return next(iv["tariff_window"] for iv in day.intervals
                    if iv["interval_start"].startswith(f"2026-05-01T{hhmm}"))

    # Cosy classifies by clock (05:00 cheap, 17:00 peak); Agile by price within
    # the day's 5..30p range (5p cheap at 05:00, 30p peak at 17:00)...
    assert at(cosy, "17:00") == "peak" and at(agile, "05:00") == "cheap"
    assert at(cosy, "05:00") == "cheap" and at(agile, "17:00") == "peak"
    assert at(cosy, "10:00") == "day" and at(agile, "10:00") == "day"
    # ...but the cost is identical (consumption × the interval's own rate).
    assert at_10(cosy, "cost_pence") == at_10(agile, "cost_pence")


def test_fetch_gas_converts_m3(monkeypatch):
    rows = [{"consumption": 1.0,
             "interval_start": "2026-05-01T00:00:00+01:00",
             "interval_end": "2026-05-01T00:30:00+01:00"}]
    c = _client(monkeypatch, {"consumption/": {"results": rows, "next": None}})
    day = c.fetch_gas("9999999999", "GAS-SER-7", "2026-05-01")
    assert day.intervals[0]["consumption_kwh"] == pytest.approx(11.2206, abs=1e-3)
    # units='kwh' must NOT convert.
    day2 = c.fetch_gas("9999999999", "GAS-SER-7", "2026-05-01", units="kwh")
    assert day2.intervals[0]["consumption_kwh"] == pytest.approx(1.0)


def test_standing_charge_is_date_aware_and_cached(monkeypatch):
    history = {"results": [
        {"value_inc_vat": 40.0, "valid_from": "2024-01-01T00:00:00Z",
         "valid_to": "2025-04-01T00:00:00Z"},
        {"value_inc_vat": 55.0, "valid_from": "2025-04-01T00:00:00Z",
         "valid_to": None},
    ], "next": None}
    c = OctopusClient(api_key="k", account_number="A-1")
    calls = {"n": 0}

    def fake_get(path, params=None):
        if "standing-charges/" in path:
            calls["n"] += 1
            return history
        return {"results": [], "next": None}

    monkeypatch.setattr(c, "_get", fake_get)
    # A day in 2024 gets the old charge; a 2026 day gets the newer one.
    old = c.get_standing_charge("COSY", "E-1R-COSY-A", "electricity",
                                datetime(2024, 6, 1, tzinfo=UTC))
    new = c.get_standing_charge("COSY", "E-1R-COSY-A", "electricity",
                                datetime(2026, 6, 1, tzinfo=UTC))
    assert old == 40.0 and new == 55.0
    # The history is fetched once and reused from the cache (not per day).
    assert calls["n"] == 1


def test_pagination_follows_next(monkeypatch):
    c = OctopusClient(api_key="sk", account_number="A-1")
    calls = {"n": 0}

    def fake_get(path, params=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"results": [{"consumption": 1}],
                    "next": "https://api.octopus.energy/v1/next-page"}
        return {"results": [{"consumption": 2}], "next": None}

    monkeypatch.setattr(c, "_get", fake_get)
    rows = c._get_paginated("/x/", {})
    assert len(rows) == 2 and calls["n"] == 2
