"""Tests for sync aggregation and daily orchestration (mocked clients)."""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import config
import database as db
import sync


def _freeze_now(monkeypatch, iso="2026-05-28T12:00:00"):
    """Freeze sync.datetime.now to a fixed Europe/London time (project rule:
    anything near schedulers/retention must never read the real clock)."""
    fixed = datetime.fromisoformat(iso).replace(tzinfo=ZoneInfo("Europe/London"))

    class _FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)

    monkeypatch.setattr(sync, "datetime", _FakeDT)
    return fixed


def _seed_day(date="2026-05-01"):
    """Insert a representative day of intervals into the (temp) DB."""
    # Solar: house load 10 kWh, pv 18, charge 12, discharge 9, SoC ramps to 100.
    solar = []
    for i in range(48):
        h, m = divmod(i * 30, 60)
        solar.append({
            "interval_start": f"{date}T{h:02d}:{m:02d}:00",
            "pv_kwh": 18.0 / 48, "battery_charge_kwh": 12.0 / 48,
            "battery_discharge_kwh": 9.0 / 48, "grid_import_kwh": None,
            "grid_export_kwh": None, "house_load_kwh": 10.0 / 48,
            "soc_pct": min(100.0, 40 + i * 1.5),
        })
    db.upsert_intervals("solar_intervals", solar)

    # Electricity import: 4 kWh total, mixed windows, 48 intervals.
    elec = []
    for i in range(48):
        h, m = divmod(i * 30, 60)
        ts = f"{date}T{h:02d}:{m:02d}:00"
        window = "cheap" if h < 7 else ("peak" if 16 <= h < 19 else "day")
        elec.append({
            "interval_start": ts, "interval_end": ts,
            "consumption_kwh": 4.0 / 48, "tariff_window": window,
            "rate_p_per_kwh": 10.0, "cost_pence": (4.0 / 48) * 10.0,
        })
    db.upsert_intervals("electricity_intervals", elec)

    # Export: 6 kWh earning 4.1p.
    export = [{"interval_start": f"{date}T12:00:00", "interval_end": f"{date}T12:30:00",
               "export_kwh": 6.0, "rate_p_per_kwh": 4.1, "earned_pence": 6.0 * 4.1}]
    db.upsert_intervals("export_intervals", export)

    # Gas: 12 kWh costing 6p/kWh.
    gas = [{"interval_start": f"{date}T07:00:00", "interval_end": f"{date}T07:30:00",
            "consumption_kwh": 12.0, "rate_p_per_kwh": 6.0, "cost_pence": 72.0}]
    db.upsert_intervals("gas_intervals", gas)


def test_intervals_for_date_range_boundaries(tmp_db):
    # Rows across three days incl. day-boundary edges and an offset suffix.
    rows = [
        {"interval_start": "2026-05-01T23:30:00+0100", "interval_end": "x",
         "export_kwh": 1.0},
        {"interval_start": "2026-05-02T00:00:00+0100", "interval_end": "x",
         "export_kwh": 2.0},
        {"interval_start": "2026-05-02T23:30:00+0100", "interval_end": "x",
         "export_kwh": 3.0},
        {"interval_start": "2026-05-03T00:00:00+0100", "interval_end": "x",
         "export_kwh": 4.0},
    ]
    db.upsert_intervals("export_intervals", rows)
    got = db.intervals_for_date("export_intervals", "2026-05-02")
    assert [r["export_kwh"] for r in got] == [2.0, 3.0]   # only the 2nd, both edges


def test_run_recent_catchup_resyncs_only_unsettled(tmp_db, monkeypatch):
    from datetime import date
    monkeypatch.setattr(sync, "_today_local", lambda: date(2026, 5, 26))
    calls = []
    monkeypatch.setattr(sync, "run_daily", lambda d=None: calls.append(d))
    db.upsert_daily_summary({"date": "2026-05-25", "data_partial": 1})  # unsettled
    db.upsert_daily_summary({"date": "2026-05-24", "data_partial": 0})  # settled
    sync.run_recent_catchup(days_back=2)
    assert "2026-05-26" in calls       # today is always synced
    assert "2026-05-25" in calls       # partial -> re-synced (self-heal)
    assert "2026-05-24" not in calls   # settled -> skipped (no API call)


def test_post_brief_if_ready_safety_net(tmp_db, monkeypatch):
    # post_brief_if_ready is the catch-up for a missed master run. Posts iff
    # last_brief_date != date (after the master slot); sets the key on success.
    _freeze_now(monkeypatch, "2026-05-28T12:00:00")   # midday — past the gate
    config.set_credential("MATTERMOST_WEBHOOK_URL", "https://mm.example/hooks/x")
    sent = []
    import brief
    import notifications
    monkeypatch.setattr(notifications, "post_brief",
                        lambda msg: (sent.append(msg) or (True, "ok")))
    monkeypatch.setattr(brief, "build_daily_brief",
                        lambda d, flex_rate_p=None: f"brief-{d}")
    # A summary must exist (fresh-install guard) for the missed-master post.
    db.upsert_daily_summary({"date": "2026-05-27", "pv_kwh": 1.0})
    db.upsert_daily_summary({"date": "2026-05-28", "pv_kwh": 1.0})

    # First call -> no last_brief_date recorded -> posts.
    assert sync.post_brief_if_ready("2026-05-27") is True
    assert sent == ["brief-2026-05-27"]
    assert db.setting_get("last_brief_date") == "2026-05-27"

    # Repeat call same date -> dedup, no second post.
    assert sync.post_brief_if_ready("2026-05-27") is False
    assert len(sent) == 1

    # Different date (next day) -> posts, updates the key.
    assert sync.post_brief_if_ready("2026-05-28") is True
    assert db.setting_get("last_brief_date") == "2026-05-28"


def test_post_brief_if_ready_never_fires_before_master_slot(tmp_db, monkeypatch):
    """The first intraday tick after midnight must NOT post a premature brief —
    the 06:25 master would then post the same day again (observed double-post)."""
    _freeze_now(monkeypatch, "2026-05-28T00:35:00")
    sent = []
    import brief
    import notifications
    monkeypatch.setattr(notifications, "post_brief",
                        lambda msg: (sent.append(msg) or (True, "ok")))
    monkeypatch.setattr(brief, "build_daily_brief",
                        lambda d, flex_rate_p=None: f"brief-{d}")
    db.upsert_daily_summary({"date": "2026-05-27", "pv_kwh": 1.0})
    assert sync.post_brief_if_ready("2026-05-27") is False
    assert sent == []
    assert db.setting_get("last_brief_date") is None


def test_post_brief_if_ready_fresh_install_sets_baseline(tmp_db, monkeypatch):
    """No dedup key AND no summary = fresh install mid-backfill: set the
    baseline quietly instead of posting a brief built from nothing."""
    _freeze_now(monkeypatch, "2026-05-28T12:00:00")
    config.set_credential("MATTERMOST_WEBHOOK_URL", "https://mm.example/hooks/x")
    sent = []
    import brief
    import notifications
    monkeypatch.setattr(notifications, "post_brief",
                        lambda msg: (sent.append(msg) or (True, "ok")))
    monkeypatch.setattr(brief, "build_daily_brief",
                        lambda d, flex_rate_p=None: f"brief-{d}")
    assert sync.post_brief_if_ready("2026-05-27") is False
    assert sent == []
    assert db.setting_get("last_brief_date") == "2026-05-27"


def test_master_skips_brief_already_posted_by_safety_net(tmp_db, monkeypatch):
    """run_daily_and_notify (scheduler path, no explicit date) must dedup
    against last_brief_date — the other half of the double-post bug."""
    sent = []
    import brief
    import notifications
    monkeypatch.setattr(notifications, "post_brief",
                        lambda msg: (sent.append(msg) or (True, "ok")))
    monkeypatch.setattr(brief, "build_daily_brief",
                        lambda d, flex_rate_p=None: f"brief-{d}")
    monkeypatch.setattr(sync, "run_daily",
                        lambda date=None: {"date": sync.yesterday(),
                                           "errors": []})
    db.setting_set("last_brief_date", sync.yesterday())
    result = sync.run_daily_and_notify()
    assert result["brief_sent"] is False
    assert sent == []
    # An explicit-date CLI run is a deliberate re-send and still posts.
    monkeypatch.setattr(sync, "run_daily",
                        lambda date=None: {"date": date, "errors": []})
    result = sync.run_daily_and_notify(sync.yesterday())
    assert result["brief_sent"] is True
    assert len(sent) == 1


def test_run_daily_and_notify_sets_dedup_key(tmp_db, monkeypatch):
    # The master run also stamps last_brief_date on a successful post so the
    # intraday safety-net stays a no-op for the rest of the day.
    import brief
    import notifications
    monkeypatch.setattr(sync, "run_daily",
                        lambda d=None: {"date": "2026-05-27", "flex_rate_p": None})
    monkeypatch.setattr(brief, "build_daily_brief", lambda d, flex_rate_p=None: "x")
    monkeypatch.setattr(notifications, "post_brief", lambda msg: (True, "ok"))
    sync.run_daily_and_notify()
    assert db.setting_get("last_brief_date") == "2026-05-27"


def test_compute_summary_import_estimated_from_sunsynk_ct(tmp_db):
    # Octopus partial (only 2/48 intervals settled) but Sunsynk CT has the
    # full day -> use Sunsynk per-interval grid_import, classify by Cosy window
    # from the timestamp, value at the cached per-window rates from a prior
    # settled day.
    # 1) Seed prior-day Octopus rates so _latest_window_rates() has data:
    db.upsert_intervals("electricity_intervals", [
        {"interval_start": "2026-05-26T04:00:00", "interval_end": "x",
         "consumption_kwh": 1.0, "tariff_window": "cheap",
         "rate_p_per_kwh": 10.0, "cost_pence": 10.0},
        {"interval_start": "2026-05-26T12:00:00", "interval_end": "x",
         "consumption_kwh": 1.0, "tariff_window": "day",
         "rate_p_per_kwh": 24.0, "cost_pence": 24.0},
        {"interval_start": "2026-05-26T17:00:00", "interval_end": "x",
         "consumption_kwh": 1.0, "tariff_window": "peak",
         "rate_p_per_kwh": 38.0, "cost_pence": 38.0}])
    # 2) Target day: only 2 Octopus rows (partial), plus 4 Sunsynk intervals.
    db.upsert_intervals("electricity_intervals", [
        {"interval_start": "2026-05-27T00:00:00", "interval_end": "x",
         "consumption_kwh": 0.05, "tariff_window": "cheap",
         "rate_p_per_kwh": 10.0, "cost_pence": 0.5}])
    db.upsert_intervals("solar_intervals", [
        {"interval_start": "2026-05-27T05:00:00", "grid_import_kwh": 2.0},  # cheap
        {"interval_start": "2026-05-27T12:00:00", "grid_import_kwh": 0.5},  # day
        {"interval_start": "2026-05-27T17:30:00", "grid_import_kwh": 0.3},  # peak
        {"interval_start": "2026-05-27T22:30:00", "grid_import_kwh": 0.1}]) # cheap
    s = sync.compute_daily_summary("2026-05-27")
    assert s["import_estimated"] == 1
    assert s["grid_import_kwh"] == 2.9          # 2.0 + 0.5 + 0.3 + 0.1
    assert s["cheap_import_kwh"] == 2.1         # 05:00 + 22:30
    assert s["day_import_kwh"] == 0.5
    assert s["peak_import_kwh"] == 0.3
    # Cost = 2.1*10 + 0.5*24 + 0.3*38 = 21 + 12 + 11.4 = 44.4p
    assert s["import_cost_pence"] == 44.4


def test_compute_summary_octopus_settled_no_estimate(tmp_db):
    # >=40 Octopus intervals -> authoritative, no estimate flag.
    db.upsert_intervals("electricity_intervals", [
        {"interval_start": f"2026-05-27T{(i // 2):02d}:{(i % 2) * 30:02d}:00",
         "interval_end": "x", "consumption_kwh": 0.1, "tariff_window": "cheap",
         "rate_p_per_kwh": 10.0, "cost_pence": 1.0} for i in range(48)])
    s = sync.compute_daily_summary("2026-05-27")
    assert s["import_estimated"] == 0
    assert s["grid_import_kwh"] == round(48 * 0.1, 4)


def test_backfill_already_stored_skip(tmp_db):
    from datetime import date, timedelta
    today = date(2026, 5, 25)
    old = date(2025, 1, 1)
    recent = today - timedelta(days=1)
    # Nothing stored -> don't skip.
    assert sync._already_stored(old, today, force=False) is False
    db.upsert_daily_summary({"date": old.isoformat(), "octopus_ok": 1,
                             "import_cost_pence": 100.0})
    db.upsert_daily_summary({"date": recent.isoformat(), "octopus_ok": 1,
                             "import_cost_pence": 100.0})
    # Stored + old -> skip; stored but within settling window -> don't skip.
    assert sync._already_stored(old, today, force=False) is True
    assert sync._already_stored(recent, today, force=False) is False
    # force overrides.
    assert sync._already_stored(old, today, force=True) is False


def test_seconds_until():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    london = ZoneInfo("Europe/London")
    # Same day, later time.
    now = datetime(2026, 5, 25, 6, 0, tzinfo=london)
    assert sync._seconds_until(now, 6, 25) == 25 * 60
    # Past the time today -> wraps to tomorrow.
    now = datetime(2026, 5, 25, 7, 0, tzinfo=london)
    assert sync._seconds_until(now, 6, 25) == 23 * 3600 + 25 * 60


def test_compute_daily_summary_aggregates(tmp_db):
    _seed_day()
    s = sync.compute_daily_summary("2026-05-01", standing_elec_p=55.0,
                                   standing_gas_p=32.0)
    assert s["pv_kwh"] == pytest.approx(18.0, abs=1e-6)
    assert s["house_load_kwh"] == pytest.approx(10.0, abs=1e-6)
    assert s["grid_import_kwh"] == pytest.approx(4.0, abs=1e-6)
    assert s["grid_export_kwh"] == pytest.approx(6.0)
    assert s["gas_kwh"] == pytest.approx(12.0)
    assert s["peak_soc_pct"] == 100.0
    assert s["self_sufficiency_pct"] == pytest.approx(60.0)  # (1-4/10)*100
    # net = import(40) + standing(55) + gas(72) + standing(32) - export(24.6)
    assert s["import_cost_pence"] == pytest.approx(40.0)
    assert s["export_earned_pence"] == pytest.approx(24.6)
    assert s["net_cost_pence"] == pytest.approx(40 + 55 + 72 + 32 - 24.6)
    # Window breakdown sums to total import.
    assert (s["cheap_import_kwh"] + s["day_import_kwh"]
            + s["peak_import_kwh"]) == pytest.approx(4.0, abs=1e-6)
    assert s["sunsynk_ok"] == 1 and s["octopus_ok"] == 1
    assert s["data_partial"] == 0


def test_compute_daily_summary_partial_flag(tmp_db):
    # Only 30 import intervals -> partial.
    rows = [{"interval_start": f"2026-05-02T00:{i:02d}:00", "interval_end": "x",
             "consumption_kwh": 0.1, "tariff_window": "day",
             "rate_p_per_kwh": 10.0, "cost_pence": 1.0} for i in range(30)]
    db.upsert_intervals("electricity_intervals", rows)
    s = sync.compute_daily_summary("2026-05-02")
    assert s["data_partial"] == 1
    assert s["sunsynk_ok"] == 0   # no solar this day


def test_export_estimate_fallback(tmp_db):
    # Partial day (Octopus export not settled) + Sunsynk export -> estimate.
    db.upsert_intervals("solar_intervals", [{
        "interval_start": f"2026-06-01T{h:02d}:00:00", "grid_export_kwh": 0.5,
        "house_load_kwh": 0.2} for h in range(10)])
    s = sync.compute_daily_summary("2026-06-01")
    assert s["grid_export_kwh"] == pytest.approx(5.0)   # 10 * 0.5
    assert s["export_estimated"] == 1
    assert s["export_earned_pence"] == 0.0
    # A handful of settled Octopus export intervals (<40) is still "partial".
    db.upsert_intervals("export_intervals", [{
        "interval_start": f"2026-06-01T{h:02d}:00:00", "interval_end": "x",
        "export_kwh": 0.1, "rate_p_per_kwh": 4.0, "earned_pence": 0.4}
        for h in range(3)])
    assert sync.compute_daily_summary("2026-06-01")["export_estimated"] == 1
    # A fully settled day (>=40 export intervals) -> Octopus authoritative.
    db.upsert_intervals("export_intervals", [{
        "interval_start": f"2026-06-02T{i//2:02d}:{(i % 2)*30:02d}:00",
        "interval_end": "x", "export_kwh": 0.2, "rate_p_per_kwh": 4.0,
        "earned_pence": 0.8} for i in range(48)])
    s2 = sync.compute_daily_summary("2026-06-02")
    assert s2["export_estimated"] == 0
    assert s2["grid_export_kwh"] == pytest.approx(48 * 0.2)
    assert s2["export_earned_pence"] == pytest.approx(48 * 0.8)


def test_export_estimate_valued_and_net_gain(tmp_db):
    # A known export rate exists from a past settled interval.
    db.upsert_intervals("export_intervals", [{
        "interval_start": "2026-05-01T00:00:00", "interval_end": "x",
        "export_kwh": 1.0, "rate_p_per_kwh": 15.0, "earned_pence": 15.0}])
    # Today: big Sunsynk export estimate, no import.
    db.upsert_intervals("solar_intervals", [{
        "interval_start": "2026-06-10T12:00:00", "grid_export_kwh": 4.0,
        "grid_import_kwh": 0.0, "house_load_kwh": 1.0}])
    s = sync.compute_daily_summary("2026-06-10")
    assert s["export_estimated"] == 1
    assert s["export_earned_pence"] == pytest.approx(60.0)   # 4 kWh * 15p
    assert s["net_cost_pence"] < 0                            # -> Net Gain


def test_compute_summary_empty_day(tmp_db):
    s = sync.compute_daily_summary("2099-01-01")
    assert s["grid_import_kwh"] is None     # unknown, not zero (2026-10-07 rule)
    assert s["self_sufficiency_pct"] is None  # no house load
    assert s["sunsynk_ok"] == 0 and s["octopus_ok"] == 0


def test_run_daily_degrades_and_stores_summary(tmp_db, monkeypatch):
    """run_daily wires sources together and persists a summary even when only
    part of the data is present."""
    fake_settings = SimpleNamespace(
        sunsynk_configured=True, octopus_configured=True,
        octopus_api_key="k", octopus_account_number="A-1",
        octopus_mpan="m", octopus_meter_serial="s",
        octopus_export_mpan="", octopus_export_serial="",
        octopus_mprn="", octopus_gas_serial="",
        sunsynk_username="u", sunsynk_password="p",
        sunsynk_plant_id="1", sunsynk_inverter_sn="sn")
    monkeypatch.setattr(config.Settings, "load", staticmethod(lambda: fake_settings))

    # Lightweight client stand-ins.
    monkeypatch.setattr(sync.sunsynk, "SunsynkClient",
                        lambda *a, **k: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(sync.octopus, "OctopusClient",
                        lambda *a, **k: SimpleNamespace(close=lambda: None))

    def fake_sunsynk_day(client, date):
        db.upsert_intervals("solar_intervals", [{
            "interval_start": f"{date}T00:00:00", "pv_kwh": 5.0,
            "house_load_kwh": 8.0, "soc_pct": 75.0}])
        return 1, sync.sunsynk.DayResult(date=date)

    def fake_octopus_day(octo, meters, date, gas_units="m3"):
        db.upsert_intervals("electricity_intervals", [{
            "interval_start": f"{date}T00:00:00", "interval_end": "x",
            "consumption_kwh": 2.0, "tariff_window": "cheap",
            "rate_p_per_kwh": 9.0, "cost_pence": 18.0}])
        return {"octopus_elec": 1}

    monkeypatch.setattr(sync, "sync_sunsynk_day", fake_sunsynk_day)
    monkeypatch.setattr(sync, "sync_octopus_day", fake_octopus_day)
    monkeypatch.setattr(sync, "resolve_meters", lambda *a, **k: sync.octopus.MeterInfo())
    monkeypatch.setattr(sync, "_standing_charges", lambda *a, **k: (55.0, 30.0))

    result = sync.run_daily("2026-05-03")
    assert result["sunsynk"] == 1
    assert result["octopus"] == {"octopus_elec": 1}
    stored = db.get_daily_summary("2026-05-03")
    assert stored is not None
    assert stored["pv_kwh"] == pytest.approx(5.0)
    assert stored["standing_elec_pence"] == pytest.approx(55.0)
    assert stored["self_sufficiency_pct"] == pytest.approx(75.0)  # (1-2/8)*100
