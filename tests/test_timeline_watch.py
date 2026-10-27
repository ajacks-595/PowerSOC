"""Quick wins 7 + 8 — the next-24h price/carbon timeline and the tariff /
agreement change watcher. Hermetic (see conftest)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import brief
import carbon
import config
import database as db
import notifications
import tariff_watch
import timeline
from octopus import MeterInfo

LONDON = ZoneInfo("Europe/London")
GO = "E-1R-GO-VAR-22-10-14-N"
NOW = datetime(2026, 10, 20, 22, 10, tzinfo=LONDON)          # BST


def _go_two_days():
    for vf, vt, p in (("2026-10-19T23:00:00Z", "2026-10-19T23:30:00Z", 27.0),
                      ("2026-10-19T23:30:00Z", "2026-10-20T04:30:00Z", 7.5),
                      ("2026-10-20T04:30:00Z", "2026-10-20T23:30:00Z", 27.0),
                      ("2026-10-20T23:30:00Z", "2026-10-21T04:30:00Z", 7.5),
                      ("2026-10-21T04:30:00Z", "2026-10-21T23:00:00Z", 27.0)):
        db.upsert_tariff_rate("electricity_import", vf, p, vt)
    db.upsert_tariff_rate("electricity_export", "2026-01-01T00:00:00Z", 15.0, None)


def test_timeline_has_48_slots_with_prices_bands_and_carbon(monkeypatch):
    config.set_text("import_tariff_code", GO)
    _go_two_days()
    start = datetime(2026, 10, 20, 21, 0, tzinfo=timezone.utc)      # 22:00 BST
    slots = [{"from": (start + timedelta(minutes=30 * i)).strftime("%Y-%m-%dT%H:%MZ"),
              "g": 100 - i, "index": "low"} for i in range(48)]
    monkeypatch.setattr(carbon, "get_intensity", lambda: {"available": True, "slots": slots})
    t = timeline.build(NOW)
    assert len(t["slots"]) == 48
    first = t["slots"][0]
    assert first["label"] == "22:00" and first["import_p"] == 27.0 and first["export_p"] == 15.0
    assert first["carbon_g"] == 100
    cheap = [s for s in t["slots"] if s["band"] == "cheap"]
    assert cheap[0]["label"] == "00:30" and len(cheap) == 10       # 00:30-05:30
    assert t["cheapest_3h"]["from_label"] == "00:30" and t["cheapest_3h"]["avg_p"] == 7.5
    assert t["greenest_3h"]["from_label"] == "19:00"               # g falls all day: last 6 slots


def test_timeline_marks_unknown_prices_not_zero():
    t = timeline.build(NOW)
    assert all(s["import_p"] is None for s in t["slots"])
    assert t["cheapest_3h"] is None


def test_timeline_routes():
    import app as app_module
    import auth
    import home_tokens
    application = app_module.create_app()
    c = application.test_client()
    db.insert_user("admin", auth.hash_password("supersecret1"), role="admin")
    tok = home_tokens.create("Home Assistant")["token"]
    r = c.get("/api/home/timeline", headers={"X-PowerSOC-Token": tok})
    assert r.status_code == 200 and len(r.get_json()["data"]["slots"]) == 48


# ---------- tariff watcher ------------------------------------------------------

def _meters(*agreements, code="E-1R-COSY-22-12-08-N"):
    return MeterInfo(import_tariff_code=code, agreements={"import": list(agreements)})


A1 = {"tariff_code": "E-1R-COSY-22-12-08-N", "valid_from": "2026-10-01T00:00:00+01:00",
      "valid_to": None}


@pytest.fixture
def posted(monkeypatch):
    config.set_credential("MATTERMOST_WEBHOOK_URL", "https://mm.example/hooks/x")
    sent = []
    monkeypatch.setattr(notifications, "post_brief", lambda msg: sent.append(msg) or (True, "ok"))
    return sent


def _rates(*vals):
    db.upsert_tariff_rate("electricity_import", "2026-10-26T00:00:00Z", vals[0],
                          "2026-10-26T04:00:00Z")
    for i, v in enumerate(vals[1:], 1):
        db.upsert_tariff_rate("electricity_import", f"2026-10-26T{4 + i * 3:02d}:00:00Z", v,
                              f"2026-10-26T{7 + i * 3:02d}:00:00Z")


def test_first_run_only_records_a_baseline(posted):
    _rates(12.19, 24.85, 37.28)
    assert tariff_watch.check(_meters(A1), today="2026-10-26") == []
    assert posted == []


def test_a_new_booked_agreement_is_announced_once(posted):
    tariff_watch.check(_meters(A1), today="2026-10-26")
    renewal = {"tariff_code": "E-1R-COSY-FIX-12M-26-10-20-N",
               "valid_from": "2026-11-01T00:00:00Z", "valid_to": None}
    notes = tariff_watch.check(_meters(A1, renewal), today="2026-10-26")
    assert len(notes) == 1 and "COSY-FIX-12M" in notes[0] and "1 Nov" in notes[0]
    assert tariff_watch.check(_meters(A1, renewal), today="2026-10-27") == []
    assert len(posted) == 1


def test_an_ending_tariff_with_nothing_booked_is_flagged(posted):
    ending = dict(A1, valid_to="2026-11-10T00:00:00Z")
    tariff_watch.check(_meters(ending), today="2026-10-01")
    notes = tariff_watch.check(_meters(ending), today="2026-10-26")
    assert len(notes) == 1 and "ends on" in notes[0] and "10 Nov" in notes[0]
    assert tariff_watch.check(_meters(ending), today="2026-10-27") == []


def test_price_changes_are_reported_but_agile_is_ignored(posted):
    _rates(12.19, 24.85, 37.28)
    tariff_watch.check(_meters(A1), today="2026-10-26")
    db.upsert_tariff_rate("electricity_import", "2026-10-26T00:00:00Z", 13.86, "2026-10-26T04:00:00Z")
    notes = tariff_watch.check(_meters(A1), today="2026-10-26")
    assert len(notes) == 1 and "12.19" in notes[0] and "13.86" in notes[0]
    db.setting_set("tariff_watch", None)                       # fresh baseline
    agile = _meters(dict(A1, tariff_code="E-1R-AGILE-24-10-01-N"), code="E-1R-AGILE-24-10-01-N")
    tariff_watch.check(agile, today="2026-10-26")
    db.upsert_tariff_rate("electricity_import", "2026-10-26T00:00:00Z", 2.0, "2026-10-26T04:00:00Z")
    assert tariff_watch.check(agile, today="2026-10-27") == []


def test_notices_appear_in_the_next_brief(posted):
    tariff_watch.check(_meters(A1), today="2026-10-25")
    renewal = {"tariff_code": "E-1R-COSY-FIX-12M-26-10-20-N",
               "valid_from": "2026-11-01T00:00:00Z", "valid_to": None}
    tariff_watch.check(_meters(A1, renewal), today="2026-10-26")
    db.setting_set("last_brief_date", "2026-10-25")
    assert "COSY-FIX-12M" in brief.build_daily_brief("2026-10-26")
