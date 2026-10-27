"""Home-consumer API (/api/home/*): token-gated, read-only, for trusted LAN
consumers like a home dashboard. Gated in auth.login_required_globally."""
from __future__ import annotations

import logging
from datetime import date as date_cls

from flask import Blueprint

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
    _live_snapshot,
    _plan_data,
    _reconcile_data,
    _tariffs_data,
)

log = logging.getLogger("powersoc.home")


# ---------- home-consumer API (token-gated, read-only) --------------------
# For trusted LAN consumers like a home dashboard. Gated in auth.login_required_
# globally via the /api/home/ path prefix (shared token, default-off).

home_bp = Blueprint("home", __name__, url_prefix="/api/home")


@home_bp.route("/live")
def home_live():
    return ok(_cached("live", config.LIVE_CACHE_SECONDS, _live_snapshot))  # shared cache


@home_bp.route("/today")
def home_today():
    return ok(_build_day_payload(sync.today()))


@home_bp.route("/day/<date>")
def home_day(date):
    return _day_response(date)


@home_bp.route("/history")
def home_history():
    from flask import request as _req
    period = _req.args.get("period", "week")
    if period not in ("week", "month", "year"):
        return err("period must be week|month|year", 400)
    try:
        anchor = date_cls.fromisoformat(_req.args.get("anchor")
                                        or sync.today())
        return ok(_build_history_payload(period, anchor))
    except (ValueError, OverflowError):
        return err("invalid anchor (expected YYYY-MM-DD)", 400)


@home_bp.route("/reconcile")
def home_reconcile():
    return ok(_reconcile_data())


@home_bp.route("/tariffs")
def home_tariffs():
    import octopus
    try:
        return ok(_cached(f"tariffs:{sync.today()}", 6 * 3600, _tariffs_data))
    except octopus.OctopusError as e:
        return err(f"tariff fetch failed: {e}", 502)


@home_bp.route("/timeline")
def home_timeline():
    """Next 24h of price + carbon per half-hour (a heating controller can schedule on it)."""
    import timeline
    return ok(_cached("timeline", 300, timeline.build))


@home_bp.route("/status")
def home_status():
    lo, hi = db.summary_date_bounds()
    return ok({"app": "PowerSOC", "earliest": lo, "latest": hi})


@home_bp.route("/weather")
def home_weather():
    import weather
    return ok(weather.get_weather())


@home_bp.route("/forecast")
def home_forecast():
    return ok(_forecast_data())


@home_bp.route("/plan")
def home_plan():
    return ok(_plan_data())


@home_bp.route("/carbon")
def home_carbon():
    import carbon
    return ok(carbon.get_intensity())


@home_bp.route("/account")
def home_account():
    return ok(_account_data())
