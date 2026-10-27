"""Session-authenticated JSON API: health + read endpoints. Admin/mutating
routes register on the SAME `api_bp` from routes/admin.py, so every endpoint
keeps its `api.*` name."""
from __future__ import annotations

import logging
from datetime import date as date_cls

from flask import Blueprint, request

import config
import database as db
import sync
from routes.common import _cached, err, ok
from routes.payloads import (
    _account_data,
    _build_day_payload,
    _build_history_payload,
    _day_response,
    _forecast_data,
    _indepth_snapshot,
    _live_snapshot,
    _plan_data,
    _reconcile_data,
    _tariffs_data,
)

log = logging.getLogger("powersoc.api")


# ---------- JSON API ------------------------------------------------------

api_bp = Blueprint("api", __name__, url_prefix="/api")


@api_bp.route("/health")
def health():
    """Public, minimal liveness probe (no auth) for uptime/container checks.
    When the in-app scheduler is on, also reports whether its heartbeat is
    recent — a silently-dead scheduler thread is otherwise invisible."""
    data = {"status": "ok", "version": config.APP_VERSION}
    if config.SCHEDULER_ENABLED:
        from datetime import datetime as _dt
        ok_tick = False
        tick = db.setting_get("scheduler_last_tick")
        if tick:
            try:
                age = (_dt.now(sync.LONDON)
                       - _dt.fromisoformat(tick)).total_seconds()
                ok_tick = age < config.INTRADAY_MINUTES * 60 * 2 + 300
            except ValueError:
                pass
        data["scheduler_ok"] = ok_tick
    bk = sync.backup_status()
    data["backup_ok"] = bk["ok"] is True
    # Same disk as the data (the Pi's single SD card): one failure loses both.
    data["backup_isolated"] = bk["isolated"]
    # False when a stored credential can't be decrypted (key changed) — the
    # app is up but a source has silently stopped syncing. HTTP stays 200 so
    # the container healthcheck doesn't restart-loop over a config problem.
    data["credentials_ok"] = not config.unreadable_credentials()
    # Delivery receipt (O2) + identity (O3): a standby reads these from its
    # peer to decide whether to cover for it.
    bs = sync.brief_status()
    data["brief_ok"] = bs["ok"]
    data["brief"] = {"last_delivered": bs["last_delivered"],
                     "last_attempt_ok": (bs["last_attempt"] or {}).get("ok")}
    data["instance"] = {"name": config.instance_name(),
                        "role": config.instance_role()}
    return ok(data)


@api_bp.route("/live")
def api_live():
    return ok(_cached("live", config.LIVE_CACHE_SECONDS, _live_snapshot))


@api_bp.route("/tariffs")
def api_tariffs():
    import octopus
    try:
        return ok(_cached(f"tariffs:{sync.today()}", 6 * 3600, _tariffs_data))
    except octopus.OctopusError as e:
        return err(f"tariff fetch failed: {e}", 502)


@api_bp.route("/indepth")
def api_indepth():
    import sunsynk
    try:
        return ok(_cached("indepth", config.INDEPTH_CACHE_SECONDS, _indepth_snapshot))
    except sunsynk.SunsynkError as e:
        return err(f"Sunsynk error: {e}", 502)


@api_bp.route("/weather")
def api_weather():
    import weather
    return ok(weather.get_weather())


@api_bp.route("/carbon")
def api_carbon():
    import carbon
    return ok(carbon.get_intensity())


@api_bp.route("/forecast")
def api_forecast():
    return ok(_forecast_data())


@api_bp.route("/plan")
def api_plan():
    return ok(_plan_data())


@api_bp.route("/account")
def api_account():
    return ok(_account_data())


@api_bp.route("/coverage")
def api_coverage():
    from routes.payloads import _coverage_data
    try:
        days = min(max(int(request.args.get("days", 90)), 1), 1100)
    except ValueError:
        return err("days must be an integer", 400)
    return ok(_coverage_data(days))


@api_bp.route("/audit")
def api_audit():
    """Audit log (admin-only by path, no-store)."""
    try:
        limit = min(max(int(request.args.get("limit", 200)), 1), 1000)
    except ValueError:
        return err("limit must be an integer", 400)
    action = request.args.get("action") or None
    return ok([{k: r[k] for k in ("id", "created_at", "username", "action", "target_type",
                                  "target_id", "details", "ip_address")}
               for r in db.audit_list(limit=limit, action=action)])


@api_bp.route("/timeline")
def api_timeline():
    import timeline
    return ok(_cached("timeline", 300, timeline.build))


@api_bp.route("/insights/baseload")
def api_baseload():
    import insights
    return ok(_cached("baseload", 1800, insights.baseload))


@api_bp.route("/insights/battery-economics")
def api_battery_economics():
    import insights
    return ok(_cached("battery-econ", 3600, insights.battery_economics))


@api_bp.route("/insights/battery-health")
def api_battery_health():
    import insights
    return ok(_cached("battery-health", 3600, insights.battery_health))


@api_bp.route("/power-quality")
def api_power_quality():
    import power_quality
    try:
        days = min(max(int(request.args.get("days", 7)), 1), 30)
    except ValueError:
        return err("days must be an integer", 400)
    return ok({"summary": power_quality.summary(days), "events": power_quality.events(30),
               "series": power_quality.series(days)})


@api_bp.route("/tariff-sim")
def api_tariff_sim():
    import tariff_sim
    return ok(tariff_sim.status())


@api_bp.route("/insights/ct-check")
def api_ct_check():
    import insights
    return ok(_cached("ct-check", 1800, insights.ct_check))


@api_bp.route("/today")
def api_today():
    return ok(_build_day_payload(sync.today()))


@api_bp.route("/day/<date>")
def api_day(date):
    return _day_response(date)


_EXPORT_TABLES = {"electricity": "electricity_intervals",
                  "export": "export_intervals",
                  "gas": "gas_intervals",
                  "solar": "solar_intervals"}


@api_bp.route("/export.csv")
def api_export_csv():
    """CSV download for spreadsheets / bill reconciliation.

    Query params: start / end (YYYY-MM-DD, optional) bound the range;
    table=daily (default) exports daily summaries, or one of
    electricity|export|gas|solar for raw half-hourly intervals."""
    import csv
    import io

    from flask import Response
    bounds = []
    for v, default in ((request.args.get("start"), "0000-01-01"),
                       (request.args.get("end"), "9999-12-31")):
        if not v:
            bounds.append(default)
            continue
        try:
            # Canonical form: fromisoformat also accepts '20261020', which
            # matched no stored row (an empty export, no error).
            bounds.append(date_cls.fromisoformat(v).isoformat())
        except ValueError:
            return err("invalid start/end (expected YYYY-MM-DD)", 400)
    start, end = bounds
    table = request.args.get("table", "daily")
    buf = io.StringIO()
    w = csv.writer(buf)
    if table == "daily":
        cols = db._SUMMARY_COLUMNS
        rows = db.daily_summaries_range(start, end)
        name = "powersoc-history.csv"
    elif table in _EXPORT_TABLES:
        tbl = _EXPORT_TABLES[table]
        cols = db._INTERVAL_COLUMNS[tbl]
        with db.conn() as c:
            rows = c.execute(
                f"SELECT * FROM {tbl} WHERE interval_start >= ? "  # noqa: S608  # nosec B608  # nosemgrep: python.flask.security.injection.tainted-sql-string.tainted-sql-string — table from allowlist
                f"AND interval_start < ? ORDER BY interval_start",
                (start, end + "T24")).fetchall()
        name = f"powersoc-{table}-intervals.csv"
    else:
        return err("table must be daily|electricity|export|gas|solar", 400)
    # Only internal column names, numbers, timestamps and our own window enum
    # are written — no user-controlled text, so no formula-injection vector.
    w.writerow(cols)  # nosemgrep: python.flask.security.injection.csv-writer-injection.csv-writer-injection
    for r in rows:
        w.writerow([r[c] for c in cols])  # nosemgrep: python.flask.security.injection.csv-writer-injection.csv-writer-injection
    return Response(buf.getvalue(), mimetype="text/csv", headers={
        "Content-Disposition": f"attachment; filename={name}"})


@api_bp.route("/sync-log")
def api_sync_log():
    """Recent fetch attempts (the data-health panel reads this)."""
    try:
        limit = min(max(int(request.args.get("limit", 50)), 1), 500)
    except ValueError:
        return err("limit must be an integer", 400)
    return ok([dict(r) for r in db.sync_log_recent(limit=limit)])


@api_bp.route("/history")
def api_history():
    from flask import request as _req
    period = _req.args.get("period", "week")
    if period not in ("week", "month", "year"):
        return err("period must be week|month|year", 400)
    anchor_s = _req.args.get("anchor") or sync.today()
    try:
        anchor = date_cls.fromisoformat(anchor_s)
        return ok(_build_history_payload(period, anchor))
    except (ValueError, OverflowError):
        # OverflowError: a syntactically valid 9999-12-31 anchor overflows the
        # week/month arithmetic — a 400, not a 500.
        return err("invalid anchor (expected YYYY-MM-DD)", 400)


@api_bp.route("/history/calendar")
def api_history_calendar():
    from routes.payloads import _calendar_data
    try:
        year = int(request.args.get("year") or sync.today_date().year)
    except ValueError:
        return err("year must be an integer", 400)
    if not 2000 <= year <= 2100:
        return err("year out of range", 400)
    return ok(_calendar_data(year))


@api_bp.route("/reconcile")
def api_reconcile():
    return ok(_reconcile_data())


@api_bp.route("/roi")
def api_roi():
    """System payback: cumulative benefit (export earned + self-supplied energy
    valued at the Flexible rate) vs the configured system cost."""
    from routes.payloads import _has_source
    cost = config.get_number("system_cost_gbp")
    if not cost:
        return ok({"configured": False})
    flex = db.latest_tariff_rate("electricity_flexible")
    if not flex:
        # Self-supplied energy is valued at the Flexible rate; without one the
        # benefit was export-only and the payback came out as decades.
        return ok({"configured": True, "available": False,
                   "system_cost_gbp": cost,
                   "reason": "Flexible rate not known yet (needs an Octopus sync)"})
    rows = [r for r in db.daily_summaries_range("0000-01-01", "9999-12-31")
            if _has_source(r)]

    def benefit_p(r):
        self_kwh = (max(0.0, r["house_load_kwh"] - r["grid_import_kwh"])
                    if r["house_load_kwh"] is not None
                    and r["grid_import_kwh"] is not None else 0.0)
        return (r["export_earned_pence"] or 0) + self_kwh * flex

    total_p = sum(benefit_p(r) for r in rows)
    recent = rows[-90:] if len(rows) > 90 else rows
    avg_daily_gbp = (sum(benefit_p(r) for r in recent) / len(recent) / 100
                     if recent else 0)
    total_gbp = round(total_p / 100, 2)
    remaining = cost - total_gbp
    return ok({
        "configured": True, "available": True, "system_cost_gbp": cost,
        "cumulative_benefit_gbp": total_gbp,
        "percent_paid_back": round(total_gbp / cost * 100, 1) if cost > 0 else None,
        "avg_daily_gbp": round(avg_daily_gbp, 2),
        "breakeven_days": (round(remaining / avg_daily_gbp)
                           if avg_daily_gbp > 0 and 0 < remaining < 1e9 else 0),
        "flex_rate_p": flex or None,
    })
