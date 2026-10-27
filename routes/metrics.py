"""Prometheus exposition at /metrics (token-gated like the home API in
auth.login_required_globally)."""
from __future__ import annotations

import logging

from flask import Blueprint

import config
import database as db
import sync
from routes.common import _cached
from routes.payloads import _backup_age_s, _live_snapshot

metrics_bp = Blueprint("metrics", __name__)

log = logging.getLogger("powersoc.metrics")


@metrics_bp.route("/metrics")
def metrics():
    """Prometheus exposition (token-gated like the home API). Reuses the live
    cache, so scraping at ~60s adds no extra Sunsynk calls."""
    from flask import Response
    lines: list[str] = []

    def g(name, val, helptext=""):
        if val is None:
            return
        if helptext:
            lines.append(f"# HELP {name} {helptext}")
            lines.append(f"# TYPE {name} gauge")
        lines.append(f"{name} {val}")

    try:
        live = _cached("live", config.LIVE_CACHE_SECONDS, _live_snapshot)
    except Exception:  # noqa: BLE001 — /metrics must render through outages
        live = {}
    s = live.get("sunsynk") or {}
    g("powersoc_pv_watts", s.get("pv_w"), "Current PV power (W)")
    g("powersoc_grid_watts", s.get("grid_w"), "Grid power (+import/-export, W)")
    g("powersoc_battery_watts", s.get("battery_w"), "Battery power (+charge/-discharge, W)")
    g("powersoc_battery_soc_percent", s.get("soc_pct"), "Battery state of charge (%)")
    g("powersoc_house_load_watts", s.get("load_w"), "House load (W)")
    today = db.get_daily_summary(sync.today())
    if today:
        g("powersoc_today_pv_kwh", today["pv_kwh"], "PV generated today (kWh)")
        g("powersoc_today_grid_import_kwh", today["grid_import_kwh"], "Grid import today (kWh)")
        g("powersoc_today_grid_export_kwh", today["grid_export_kwh"], "Grid export today (kWh)")
        g("powersoc_today_gas_kwh", today["gas_kwh"], "Gas today (kWh)")
        g("powersoc_today_net_cost_pence", today["net_cost_pence"], "Net cost today (pence)")
        g("powersoc_today_self_sufficiency_percent", today["self_sufficiency_pct"],
          "Self-sufficiency today (%)")
    tw = live.get("tariff") or {}
    g("powersoc_import_rate_pence", tw.get("import_rate_p"), "Current import rate (p/kWh)")
    import carbon
    ci = carbon.get_intensity()
    if ci.get("current"):
        g("powersoc_carbon_intensity_grams", ci["current"]["g"], "Grid carbon intensity (gCO2/kWh)")
    # Data-freshness gauges so Grafana can alert on a stale pipeline rather
    # than inferring health from scrape failures.
    from datetime import datetime as _dt
    with db.conn() as c:
        for src, metric in (("sunsynk", "powersoc_sunsynk_last_ok_age_seconds"),
                            ("octopus_elec", "powersoc_octopus_last_ok_age_seconds")):
            r = c.execute("SELECT run_at FROM sync_log WHERE source=? AND "
                          "status IN ('ok','partial') ORDER BY id DESC LIMIT 1",
                          (src,)).fetchone()
            if r:
                try:
                    age = (_dt.now(sync.LONDON)
                           - _dt.fromisoformat(r["run_at"])).total_seconds()
                    g(metric, round(age), f"Seconds since last good {src} sync")
                except (ValueError, TypeError):
                    pass
    age = _backup_age_s()
    if age is not None:
        g("powersoc_last_backup_age_seconds", round(age),
          "Seconds since the last verified DB backup")
    g("powersoc_backup_dir_isolated", 1 if config.backup_dir_isolated() else 0,
      "1 when backups live outside the data directory")
    g("powersoc_key_seed_external", 1 if config.key_seed_is_external() else 0,
      "1 when the credential key seed is NOT stored in the data directory")
    return Response("\n".join(lines) + "\n", mimetype="text/plain; version=0.0.4")
