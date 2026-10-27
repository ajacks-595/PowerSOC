"""Tests for intraday alert evaluation + dedup (no live calls)."""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import alerts
import config
import database as db


def _recent(days_ago: int = 1) -> str:
    """A date relative to *now* (within the prune window). Tests must never
    hard-code an absolute date — `_prune_old` drops dedup records older than
    keep_days against the real clock, so a literal past date is a time-bomb
    that goes red once it ages out (see project CLAUDE.md, 2026-06-04)."""
    return (datetime.now(alerts.LONDON).date() - timedelta(days=days_ago)).isoformat()


def test_evaluate_conditions():
    st = SimpleNamespace(sunsynk_configured=True)
    s = {"end_soc_pct": 8, "net_cost_pence": 600, "peak_import_kwh": 3.0,
         "sunsynk_ok": 1}
    keys = dict(alerts.evaluate(s, st, hour=14))
    assert "battery_low" in keys and "high_cost" in keys and "high_peak" in keys
    assert "sunsynk_offline" not in keys     # sunsynk_ok -> not offline


def test_evaluate_offline_only_after_noon():
    st = SimpleNamespace(sunsynk_configured=True)
    s = {"sunsynk_ok": 0, "end_soc_pct": 50, "net_cost_pence": 0, "peak_import_kwh": 0}
    assert "sunsynk_offline" in dict(alerts.evaluate(s, st, hour=13))
    assert "sunsynk_offline" not in dict(alerts.evaluate(s, st, hour=9))


def test_evaluate_quiet_when_healthy():
    st = SimpleNamespace(sunsynk_configured=True)
    s = {"end_soc_pct": 80, "net_cost_pence": 120, "peak_import_kwh": 0.3,
         "sunsynk_ok": 1}
    assert alerts.evaluate(s, st, hour=14) == []


def test_check_alerts_dedups_per_day(tmp_db, monkeypatch):
    monkeypatch.setattr(config.Settings, "load", staticmethod(lambda: SimpleNamespace(
        mattermost_configured=True, sunsynk_configured=True)))
    sent = []
    monkeypatch.setattr(alerts.notifications, "post_brief",
                        lambda msg, **k: (sent.append(msg), (True, "ok"))[1])
    date = _recent(1)
    db.upsert_daily_summary({"date": date, "end_soc_pct": 5,
                             "net_cost_pence": 0, "peak_import_kwh": 0, "sunsynk_ok": 1})
    fired = alerts.check_alerts(date)
    assert "battery_low" in fired and len(sent) == 1
    # Same condition next hour -> not re-sent.
    assert alerts.check_alerts(date) == [] and len(sent) == 1


def test_check_alerts_dedup_survives_prune_of_older_day(tmp_db, monkeypatch):
    """Dedup must hold even when the day being processed is older than the
    prune window — _fired is read before _prune_old (regression for the
    prune-before-read bug, see CLAUDE.md 2026-06-04)."""
    monkeypatch.setattr(config.Settings, "load", staticmethod(lambda: SimpleNamespace(
        mattermost_configured=True, sunsynk_configured=True)))
    sent = []
    monkeypatch.setattr(alerts.notifications, "post_brief",
                        lambda msg, **k: (sent.append(msg), (True, "ok"))[1])
    old = _recent(30)   # well past keep_days=7
    db.upsert_daily_summary({"date": old, "end_soc_pct": 5,
                             "net_cost_pence": 0, "peak_import_kwh": 0, "sunsynk_ok": 1})
    assert "battery_low" in alerts.check_alerts(old) and len(sent) == 1
    # Second call: the record may be pruned AFTER being read, so this call must
    # still see it and NOT re-send.
    assert alerts.check_alerts(old) == [] and len(sent) == 1
    # THIRD call: the old bug — prune deleted the record on call 2 without
    # re-marking, so call 3 saw an empty fired-set and re-sent the alert.
    # _prune_old(keep_date=...) must protect the day being processed.
    assert alerts.check_alerts(old) == [] and len(sent) == 1


def test_check_alerts_noop_without_webhook(tmp_db, monkeypatch):
    monkeypatch.setattr(config.Settings, "load", staticmethod(lambda: SimpleNamespace(
        mattermost_configured=False, sunsynk_configured=True)))
    db.upsert_daily_summary({"date": _recent(1), "end_soc_pct": 5, "sunsynk_ok": 1})
    assert alerts.check_alerts(_recent(1)) == []


def test_prune_old_alerts_fired(tmp_db):
    db.setting_set("alerts_fired:2000-01-01", '["battery_low"]')   # ancient
    db.setting_set("alerts_fired:2099-01-01", '["high_cost"]')     # far future
    db.setting_set("cred:OCTOPUS_API_KEY", "x")                    # unrelated key
    alerts._prune_old(keep_days=7)
    assert db.setting_get("alerts_fired:2000-01-01") is None       # pruned
    assert db.setting_get("alerts_fired:2099-01-01") is not None   # kept
    assert db.setting_get("cred:OCTOPUS_API_KEY") == "x"           # untouched
