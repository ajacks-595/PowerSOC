"""Derived insights from stored data (no upstream calls): the always-on
baseload, battery time-to-reserve/full, and the meter vs CT cross-check.
Pure functions of the DB + their inputs, so each is unit-tested directly."""
from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import database as db

LONDON = ZoneInfo("Europe/London")


def _today() -> date:
    return datetime.now(LONDON).date()


def _now() -> datetime:
    return datetime.now(LONDON)


def _percentile(vals: list[float], p: float) -> float:
    s = sorted(vals)
    k = (len(s) - 1) * p / 100.0
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def _effective_import_rate(start: str, end: str) -> float | None:
    """p/kWh actually paid for imported energy over a range (priced days)."""
    rows = [r for r in db.daily_summaries_range(start, end)
            if r["import_cost_pence"] is not None and (r["grid_import_kwh"] or 0) > 0]
    kwh = sum(r["grid_import_kwh"] for r in rows)
    return round(sum(r["import_cost_pence"] for r in rows) / kwh, 3) if kwh else None


# ---------- 1. baseload ----------------------------------------------------------

def baseload(days: int = 30) -> dict:
    """The always-on draw: each day's 20th-percentile half-hourly house load
    between 00:00 and 05:00 (the quietest hours), in watts, and what that
    costs over a year at the import price actually paid."""
    end = _today()
    start = end - timedelta(days=days)
    by_day: dict[str, list[float]] = defaultdict(list)
    for r in db.intervals_between("solar_intervals", start.isoformat(), end.isoformat()):
        if r["house_load_kwh"] is not None and r["interval_start"][11:13] < "05":
            by_day[r["interval_start"][:10]].append(r["house_load_kwh"])
    trend = [{"date": d, "w": round(_percentile(v, 20) * 2000)}
             for d, v in sorted(by_day.items()) if len(v) >= 6]
    if not trend:
        return {"available": False}
    watts = round(statistics.median(t["w"] for t in trend))
    annual_kwh = round(watts * 8.766, 1)                  # W × 8,766 h / 1000
    rate = _effective_import_rate(start.isoformat(), end.isoformat())
    return {"available": True, "watts": watts, "latest_w": trend[-1]["w"],
            "trend": trend, "annual_kwh": annual_kwh, "rate_p": rate,
            "annual_cost_pence": round(annual_kwh * rate, 1) if rate else None}


# ---------- 2. battery ETA ----------------------------------------------------------

def battery_eta(*, soc_pct, battery_w, nameplate_kwh, reserve_pct,
                now: datetime | None = None) -> dict | None:
    """Minutes until the battery reaches its reserve (discharging) or full
    (charging) at the current power. None when idle or inputs are unknown."""
    if soc_pct is None or battery_w is None or not nameplate_kwh or abs(battery_w) < 50:
        return None
    reserve = reserve_pct if reserve_pct is not None else 10
    if battery_w < 0:
        kind, kwh = "reserve", max(0.0, (soc_pct - reserve) / 100 * nameplate_kwh)
    else:
        kind, kwh = "full", max(0.0, (100 - soc_pct) / 100 * nameplate_kwh)
    minutes = round(kwh / (abs(battery_w) / 1000) * 60)
    at = (now or _now()) + timedelta(minutes=minutes)
    return {"kind": kind, "minutes": minutes, "at": at.isoformat(),
            "reserve_pct": reserve}


# ---------- 5. meter vs CT ------------------------------------------------------------

def ct_check(days: int = 30) -> dict:
    """Daily grid import from the Octopus meter vs the Sunsynk CT, on days
    both are complete. The CT stands in for the meter until it settles (the
    brief's 06:25 import is CT-based), so a drifting CT skews every
    provisional figure. Flags days more than 5% apart."""
    from sync import expected_intervals
    end = _today()
    start = (end - timedelta(days=days)).isoformat()
    meter, ct = defaultdict(list), defaultdict(list)
    for r in db.intervals_between("electricity_intervals", start, end.isoformat()):
        meter[r["interval_start"][:10]].append(r["consumption_kwh"] or 0)
    for r in db.intervals_between("solar_intervals", start, end.isoformat()):
        if r["grid_import_kwh"] is not None:
            ct[r["interval_start"][:10]].append(r["grid_import_kwh"])
    out = []
    for d in sorted(set(meter) & set(ct)):
        exp = expected_intervals(d)
        m, c = sum(meter[d]), sum(ct[d])
        if len(meter[d]) >= exp and len(ct[d]) >= exp - 2 and m >= 0.5:
            out.append({"date": d, "meter_kwh": round(m, 2), "ct_kwh": round(c, 2),
                        "ratio": round(c / m, 3)})
    if not out:
        return {"available": False, "days": []}
    med = round(statistics.median(d["ratio"] for d in out), 3)
    pct = abs(med - 1) * 100
    summary = (f"CT reads {pct:.1f}% {'high' if med > 1 else 'low'} vs the meter "
               f"(median of {len(out)} days)" if pct >= 0.05 else
               f"CT matches the meter (median of {len(out)} days)")
    return {"available": True, "days": out, "median_ratio": med, "summary": summary,
            "flagged": [d for d in out if abs(d["ratio"] - 1) > 0.05]}


# ---------- 9. battery economics ----------------------------------------------------

def _slot(ts: str) -> str:
    return ts[11:16]


def battery_day(d: str) -> dict:
    """What the battery earned on a local day, half-hour by half-hour:
    discharge serving the house is worth the import price it avoided (or the
    export price, for the part that was exported); charging costs the import
    price for energy taken from the grid, or the export price forgone for
    energy taken from solar. A half-hour whose import price is unknown is
    skipped and counted, never valued at 0p."""
    import sync
    from octopus import rate_for
    imp = {_slot(r["interval_start"]): r["rate_p_per_kwh"]
           for r in db.intervals_for_date("electricity_intervals", d)
           if r["rate_p_per_kwh"] is not None}
    exp = {_slot(r["interval_start"]): r["rate_p_per_kwh"]
           for r in db.intervals_for_date("export_intervals", d)
           if r["rate_p_per_kwh"] is not None}
    periods = db.tariff_periods_for_local_day("electricity_import", date.fromisoformat(d))
    exp_fallback = sync._latest_export_rate() or 0.0     # no export tariff: nothing forgone
    value = cost = dis = c_grid_t = c_pv_t = 0.0
    unpriced = 0
    for r in db.intervals_for_date("solar_intervals", d):
        dch, ch = r["battery_discharge_kwh"] or 0.0, r["battery_charge_kwh"] or 0.0
        if dch <= 0 and ch <= 0:
            continue
        s = _slot(r["interval_start"])
        p_imp = imp.get(s)
        if p_imp is None and periods:
            ts = datetime.fromisoformat(r["interval_start"]).replace(tzinfo=LONDON)
            hit = rate_for(ts, periods)
            p_imp = hit["value_inc_vat"] if hit else None
        p_exp = exp.get(s, exp_fallback)
        d_exp = min(dch, r["grid_export_kwh"] or 0.0)
        c_grid = min(ch, r["grid_import_kwh"] or 0.0)
        if p_imp is None and (dch - d_exp > 0 or c_grid > 0):
            unpriced += 1
            continue
        value += (dch - d_exp) * (p_imp or 0) + d_exp * p_exp
        cost += c_grid * (p_imp or 0) + (ch - c_grid) * p_exp
        dis += dch
        c_grid_t += c_grid
        c_pv_t += ch - c_grid
    return {"date": d, "value_p": round(value, 2), "cost_p": round(cost, 2),
            "earned_p": round(value - cost, 2), "discharge_kwh": round(dis, 3),
            "charge_grid_kwh": round(c_grid_t, 3), "charge_pv_kwh": round(c_pv_t, 3),
            "unpriced_slots": unpriced}


def _solar_days() -> list[str]:
    with db.conn() as c:
        return [r[0] for r in c.execute(
            "SELECT DISTINCT substr(interval_start, 1, 10) FROM solar_intervals ORDER BY 1")]


def battery_economics() -> dict:
    """Lifetime / this month / last 30 days of battery earnings (feature 9)."""
    days = [d for d in _solar_days() if d < _today().isoformat()]
    if not days:
        return {"available": False}
    rows = [battery_day(d) for d in days]
    month = _today().replace(day=1).isoformat()
    last30 = (_today() - timedelta(days=30)).isoformat()
    dis = sum(r["discharge_kwh"] for r in rows)
    total = sum(r["earned_p"] for r in rows)
    nameplate = effective_capacity_kwh()
    return {"available": True, "all_time_p": round(total, 2),
            "this_month_p": round(sum(r["earned_p"] for r in rows if r["date"] >= month), 2),
            "last_30d_p": round(sum(r["earned_p"] for r in rows if r["date"] >= last30), 2),
            "per_kwh_p": round(total / dis, 2) if dis else None,
            "cycles": round(dis / nameplate, 1) if nameplate else None,
            "since": days[0], "days": rows[-60:]}


# ---------- 10. measured capacity / state of health ------------------------------------

def capacity_estimate(d: str, min_drop: float = 30.0) -> dict | None:
    """Usable capacity from the day's largest uninterrupted SoC fall:
    kWh delivered over that stretch / (SoC drop / 100). Needs a fall of at
    least `min_drop` percentage points (small swings are all noise)."""
    rows = [r for r in db.intervals_for_date("solar_intervals", d) if r["soc_pct"] is not None]
    best = None
    i = 0
    while i < len(rows) - 1:
        j, out = i, 0.0
        while j + 1 < len(rows) and rows[j + 1]["soc_pct"] <= rows[j]["soc_pct"]:
            j += 1
            out += (rows[j]["battery_discharge_kwh"] or 0) - (rows[j]["battery_charge_kwh"] or 0)
        drop = rows[i]["soc_pct"] - rows[j]["soc_pct"]
        if drop >= min_drop and out > 0 and (best is None or drop > best["drop_pct"]):
            best = {"date": d, "kwh": round(out / (drop / 100), 2), "drop_pct": round(drop)}
        i = max(j, i + 1)
    return best


def battery_health(days: int = 120) -> dict:
    import config
    since = (_today() - timedelta(days=days)).isoformat()
    est = [e for d in _solar_days() if d >= since and (e := capacity_estimate(d))]
    nameplate = config.get_number("battery_nameplate_kwh")
    trend = round(statistics.median(e["kwh"] for e in est[-14:]), 2) if est else None
    return {"available": bool(est), "estimates": est, "trend_kwh": trend,
            "nameplate_kwh": nameplate,
            "soh_pct": round(trend / nameplate * 100, 1) if trend and nameplate else None,
            "snapshots": battery_snapshots()}


def record_battery_snapshot(indepth: dict, day: str | None = None) -> bool:
    """Keep one BMS reading a day (capacity, corrected capacity, voltage…)
    so the state-of-health trend has the inverter's own view alongside ours."""
    import json
    b = (indepth or {}).get("battery") or {}
    keep = {k: b.get(k) for k in ("capacity_ah", "corrected_capacity_ah", "voltage", "soc",
                                  "bms_temp", "temp", "total_charge_kwh", "total_discharge_kwh")}
    if not any(v is not None for v in keep.values()):
        return False
    with db.conn() as c:
        cur = c.execute("INSERT OR IGNORE INTO battery_snapshots(date, data) VALUES(?,?)",
                        (day or _today().isoformat(), json.dumps(keep)))
        return cur.rowcount == 1


def battery_snapshots(limit: int = 400) -> list[dict]:
    import json
    with db.conn() as c:
        rows = c.execute("SELECT date, data FROM battery_snapshots ORDER BY date DESC LIMIT ?",
                         (limit,)).fetchall()
    return [{"date": r["date"], **json.loads(r["data"])} for r in reversed(rows)]


_capacity_cache: dict = {"ts": 0.0, "kwh": None}


def effective_capacity_kwh() -> float | None:
    """The configured nameplate, else the MEASURED usable capacity (median of
    the last 14 SoC-swing estimates in 60 days, ≥3 needed) — so the planner,
    the live ETA and the cycle count work before anyone types a nameplate in
    (the Pi's BMS reports 100 Ah, which says nothing about the bank)."""
    import time

    import config
    nameplate = config.get_number("battery_nameplate_kwh")
    if nameplate:
        return nameplate
    if time.time() - _capacity_cache["ts"] < 3600:
        return _capacity_cache["kwh"]
    since = (_today() - timedelta(days=60)).isoformat()
    est = [e["kwh"] for d in _solar_days() if d >= since and (e := capacity_estimate(d))]
    kwh = round(statistics.median(est[-14:]), 2) if len(est) >= 3 else None
    _capacity_cache.update(ts=time.time(), kwh=kwh)
    return kwh
