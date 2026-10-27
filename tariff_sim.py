"""Tariff simulator (feature 14): what this home's actual half-hourly import
would have cost on Octopus's current alternatives — Agile, Go, Flux, Cosy,
Flexible, Tracker — over the last N days, against what was actually paid.

Same usage, half-hour by half-hour: load-shifting (e.g. charging the battery
in Go's cheap window instead of Cosy's) is NOT modelled, so time-of-use
tariffs are judged on today's habits. Fetches one rates request per tariff
(public product data), in a background job; the result is stored."""
from __future__ import annotations

import json
import logging
import threading
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import config
import database as db
import octopus

log = logging.getLogger("powersoc.tariff_sim")
LONDON = ZoneInfo("Europe/London")
_KEY = "tariff_sim"
# Import products worth comparing, by display-name prefix (fixed-price
# products are excluded: their price is a one-off quote, not a time series).
CANDIDATES = ("Agile Octopus", "Octopus Go", "Octopus Flux", "Cosy Octopus",
              "Flexible Octopus", "Octopus Tracker")

_lock = threading.Lock()
_state = {"running": False}


def _today() -> date:
    return datetime.now(LONDON).date()


def _products(octo, at: datetime) -> dict[str, str]:
    out = {}
    for p in octo._get_paginated("/products/", {
            "brand": "OCTOPUS_ENERGY", "is_variable": "true",
            "available_at": at.astimezone(timezone.utc).isoformat()}):
        name = p.get("display_name") or ""
        if p.get("direction", "IMPORT") != "IMPORT":
            continue
        for want in CANDIDATES:
            if name.startswith(want) and want not in out:
                out[want] = p.get("code")
    return out


def run(days: int = 90) -> dict:
    """Price the last `days` of metered import on each candidate tariff."""
    import sync
    end = _today()
    start = end - timedelta(days=days)
    rows = [r for r in db.intervals_between("electricity_intervals", start.isoformat(),
                                            end.isoformat())]
    if not rows:
        return {"error": "no metered import in that period", "tariffs": []}
    settings = config.Settings.load()
    if not settings.octopus_configured:
        return {"error": "Octopus not configured", "tariffs": []}
    usage = [(sync._interval_epoch(r["interval_start"]), r["consumption_kwh"] or 0.0) for r in rows]
    day_list = sorted({r["interval_start"][:10] for r in rows})
    summaries = {s["date"]: s for s in db.daily_summaries_range(day_list[0], day_list[-1])}
    actual_import = sum(r["cost_pence"] for r in rows if r["cost_pence"] is not None)
    actual_standing = sum(summaries[d]["standing_elec_pence"] or 0 for d in day_list
                          if d in summaries)
    octo = octopus.OctopusClient(settings.octopus_api_key, settings.octopus_account_number)
    try:
        meters = sync.resolve_meters(settings, octo)
        region = meters.region
        if not region:
            return {"error": "region unknown (run Discover in Settings)", "tariffs": []}
        pf = datetime.fromtimestamp(min(u[0] for u in usage), timezone.utc)
        pt = datetime.fromtimestamp(max(u[0] for u in usage) + 1800, timezone.utc)
        out = []
        for name, product in _products(octo, datetime.now(timezone.utc)).items():
            code = f"E-1R-{product}-{region}"
            try:
                idx = sync._RateIndex(octo.get_unit_rates(product, code, "electricity",
                                                          pf.isoformat(), pt.isoformat()))
                priced = cost = 0.0
                for ep, kwh in usage:
                    p = idx.at(ep)
                    if p is not None:
                        cost += kwh * p
                        priced += kwh
                standing = 0.0
                for d in day_list:
                    noon = datetime.fromisoformat(d).replace(hour=12, tzinfo=LONDON)
                    standing += octo.get_standing_charge(product, code, "electricity", noon) or 0
            except octopus.OctopusError as e:
                log.warning("Tariff sim: %s failed: %s", code, e)
                continue
            total_kwh = sum(k for _, k in usage)
            out.append({"name": name, "product": product, "code": code,
                        "import_p": round(cost, 1), "standing_p": round(standing, 1),
                        "total_p": round(cost + standing, 1),
                        "coverage": round(priced / total_kwh, 3) if total_kwh else None})
    finally:
        octo.close()
    actual_total = actual_import + actual_standing
    for t in out:
        t["delta_p"] = round(t["total_p"] - actual_total, 1)
    out.sort(key=lambda t: t["total_p"])
    return {"error": None, "tariffs": out, "days": len(day_list),
            "from": day_list[0], "to": day_list[-1],
            "kwh": round(sum(k for _, k in usage), 1),
            "actual": {"import_p": round(actual_import, 1),
                       "standing_p": round(actual_standing, 1),
                       "total_p": round(actual_total, 1)},
            "note": "Same half-hourly usage on every tariff — load-shifting is not modelled."}


def _job(days: int) -> None:
    try:
        res = run(days)
    except Exception as e:  # noqa: BLE001 — never let the thread die silently
        log.exception("Tariff simulation failed")
        res = {"error": str(e), "tariffs": []}
    res["ran_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    db.setting_set(_KEY, json.dumps(res))
    with _lock:
        _state["running"] = False


def start_async(days: int = 90) -> bool:
    with _lock:
        if _state["running"]:
            return False
        _state["running"] = True
    threading.Thread(target=_job, args=(days,), name="powersoc-tariff-sim",
                     daemon=True).start()
    return True


def status() -> dict:
    try:
        result = json.loads(db.setting_get(_KEY) or "null")
    except (ValueError, TypeError):
        result = None
    with _lock:
        running = _state["running"]
    return {"running": running, "result": result}
