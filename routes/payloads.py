"""Data builders behind the JSON API (and the home API / metrics): day and
history payloads, live/in-depth snapshots, tariffs, forecast, plan, account,
reconciliation. Pure functions of the DB + upstream clients — no routing."""
from __future__ import annotations

import logging
from datetime import date as date_cls
from datetime import timedelta

import config
import database as db
import sync
from routes.common import _cached, err, ok

log = logging.getLogger("powersoc.payloads")


def _backup_age_s() -> float | None:
    """Seconds since the last verified backup, or None if never."""
    from datetime import datetime as _dt
    raw = db.setting_get("last_backup_at")
    if not raw:
        return None
    try:
        return (_dt.now(_dt.fromisoformat(raw).tzinfo)
                - _dt.fromisoformat(raw)).total_seconds()
    except (ValueError, TypeError):
        return None


# 48 half-hour slot labels: 00:00, 00:30, ... 23:30
_SLOT_LABELS = [f"{h:02d}:{m:02d}" for h in range(24) for m in (0, 30)]


def _slot_index(interval_start: str) -> int | None:
    """Map an interval_start (any of our stored ISO formats) to a 0..47 slot."""
    try:
        time_part = interval_start.split("T", 1)[1]
        hh = int(time_part[0:2])
        mm = int(time_part[3:5])
    except (IndexError, ValueError):
        return None
    idx = hh * 2 + (1 if mm >= 30 else 0)
    return idx if 0 <= idx < 48 else None


def _slot_array(rows, key, scale=1.0):
    """Place a column's values onto the 48-slot grid (summing collisions)."""
    arr = [None] * 48
    for r in rows:
        idx = _slot_index(r["interval_start"])
        if idx is None or r[key] is None:
            continue
        arr[idx] = round((arr[idx] or 0) + r[key] * scale, 4)
    return arr


def _build_day_payload(date: str) -> dict:
    solar = db.intervals_for_date("solar_intervals", date)
    elec = db.intervals_for_date("electricity_intervals", date)
    export = db.intervals_for_date("export_intervals", date)
    gas = db.intervals_for_date("gas_intervals", date)

    # Per-window import cost arrays (for the colour-coded stacked bar).
    def _window_cost(window):
        rows = [r for r in elec if r["tariff_window"] == window]
        return _slot_array(rows, "cost_pence")

    summary = db.get_daily_summary(date)
    summary_dict = dict(summary) if summary else sync.compute_daily_summary(date)

    # Export chart: Octopus once settled (>=40 intervals), else the Sunsynk CT
    # estimate from solar_intervals — so today's chart isn't empty while Octopus
    # export lags (consistent with the Exported summary box).
    if len(export) >= 40:
        export_arr, export_est = _slot_array(export, "export_kwh"), False
    else:
        sol_export = _slot_array(solar, "grid_export_kwh")
        if any(v for v in sol_export):
            export_arr, export_est = sol_export, True
        else:
            export_arr, export_est = _slot_array(export, "export_kwh"), False

    has_data = bool(solar or elec or export or gas)
    return {
        "date": date,
        "labels": _SLOT_LABELS,
        "summary": summary_dict,
        "cost_status": cost_status(summary_dict, has_data),
        # 7-full-day averages so the dashboard cards can show ▲/▼ context.
        "avg7": _avg7() if date == sync.today() else None,
        "solar": {
            "pv": _slot_array(solar, "pv_kwh"),
            "battery_discharge": _slot_array(solar, "battery_discharge_kwh"),
            "battery_charge": _slot_array(solar, "battery_charge_kwh"),
            "grid_import": _slot_array(solar, "grid_import_kwh"),
            "house_load": _slot_array(solar, "house_load_kwh"),
            "soc": _slot_array(solar, "soc_pct"),
        },
        "electricity": {
            "consumption": _slot_array(elec, "consumption_kwh"),
            "cost": _slot_array(elec, "cost_pence"),
            "cheap": _window_cost("cheap"),
            "day": _window_cost("day"),
            "peak": _window_cost("peak"),
        },
        "export": {"export_kwh": export_arr, "estimated": export_est},
        "gas": {"gas_kwh": _slot_array(gas, "consumption_kwh")},
        "nav": _nav_dates(date),
        "has_data": has_data,
    }


def cost_status(s: dict, has_data: bool) -> str:
    """How firm a day's cost is, for one consistent label on every surface:
    no_data | unpriced (a figure exists but its price doesn't) | provisional
    (estimated or still settling) | confirmed. "confirmed" used to look only
    at export_estimated, so a CT-estimated or partial day said "confirmed"."""
    if not has_data:
        return "no_data"
    if s.get("net_cost_pence") is None:
        return "unpriced"
    if (s.get("data_partial") or s.get("import_estimated")
            or s.get("export_estimated") or not s.get("octopus_ok")):
        return "provisional"
    return "confirmed"


def _has_source(r) -> bool:
    """A summary row backed by data. Older versions stored an all-zero row
    (both source flags 0) for days nothing was fetched — those must not
    count as real zero-cost, zero-energy days in averages and totals."""
    if r["sunsynk_ok"] or r["octopus_ok"] or r["import_estimated"]:
        return True
    return any((r[k] or 0) != 0 for k in (
        "pv_kwh", "grid_import_kwh", "grid_export_kwh", "gas_kwh",
        "house_load_kwh", "import_cost_pence", "export_earned_pence"))


def _avg7() -> dict:
    """Averages over the last 7 FULL days (yesterday backwards) for the
    compare-to-typical card deltas."""
    t = sync.today_date()
    rows = db.daily_summaries_range((t - timedelta(days=7)).isoformat(),
                                    (t - timedelta(days=1)).isoformat())
    rows = [r for r in rows if _has_source(r)]
    out = {}
    for k in ("net_cost_pence", "pv_kwh", "grid_import_kwh",
              "grid_export_kwh", "house_load_kwh"):
        vals = [r[k] for r in rows if r[k] is not None]
        if vals:
            out[k] = round(sum(vals) / len(vals), 2)
    return out


def _nav_dates(date: str) -> dict:
    """Prev/next/today helpers for the date navigator, bounded by stored data."""
    lo, hi = db.summary_date_bounds()
    today = sync.today()
    d = date_cls.fromisoformat(date)
    prev_d = (d - timedelta(days=1)).isoformat()
    next_d = (d + timedelta(days=1)).isoformat()
    return {
        "prev": prev_d if (lo is None or prev_d >= lo) else None,
        "next": next_d if next_d <= today else None,
        "today": today,
        "is_today": date == today,
        "earliest": lo, "latest": hi,
    }


def _live_snapshot() -> dict:
    """Near-real-time snapshot: Sunsynk power flows (live) + the current Octopus
    tariff window/rate (live-ish). Metered consumption is NOT live (settlement
    lag) so it's not included here."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    import octopus
    settings = config.Settings.load()
    out = {"ts": datetime.now(ZoneInfo("Europe/London")).isoformat(),
           "sunsynk": None, "sunsynk_error": None, "tariff": None}

    if settings.sunsynk_configured:
        import sunsynk
        sc = sunsynk.SunsynkClient(settings)
        try:
            out["sunsynk"] = sc.get_realtime()
            out["battery_eta"] = battery_eta_for(out["sunsynk"])
            try:
                import power_quality
                power_quality.record(out["sunsynk"])
            except Exception:  # noqa: BLE001 — the log must never break the live view
                log.exception("Power-quality sample failed")
        except sunsynk.SunsynkError as e:
            out["sunsynk_error"] = str(e)
        finally:
            sc.close()
    else:
        out["sunsynk_error"] = "Sunsynk not configured"

    now = datetime.now(ZoneInfo("Europe/London"))
    imp = db.rate_at("electricity_import", now.isoformat())
    exp = db.rate_at("electricity_export", now.isoformat())
    imp_p = imp["rate_p_per_kwh"] if imp else None
    exp_p = exp["rate_p_per_kwh"] if exp else None
    code = config.get_text("import_tariff_code") or None
    is_cosy = code is None or octopus.is_cosy_product(code)
    # "Window ends in 38 min" countdown — the single most actionable number for
    # load-shifting. The stored rate period's own end is exact on ANY tariff;
    # fixed Cosy boundaries are only a fallback (and only for Cosy).
    window_until = None
    if imp and imp["valid_to"]:
        try:
            window_until = octopus.to_london(imp["valid_to"])
        except (ValueError, OverflowError):
            window_until = None
    if window_until is None and is_cosy:
        h = now.hour + now.minute / 60.0
        nxt_h = next(b for b in (4, 7, 13, 16, 19, 22, 24) if b > h)
        window_until = datetime.combine(now.date(), datetime.min.time(),
                                        now.tzinfo) + timedelta(hours=nxt_h)
        window_until = window_until.replace(tzinfo=now.tzinfo)
    day_rates = db.rates_for_local_day("electricity_import", now.date())
    out["tariff"] = {
        "window": octopus.classify_window(now, imp_p, day_rates, code),
        "window_until": window_until.isoformat() if window_until else None,
        "import_rate_p": imp_p,
        "export_rate_p": exp_p,
        # True when exporting currently pays more than the import rate costs —
        # i.e. don't grid-charge, sell instead.
        "export_gt_import": (exp_p is not None and imp_p is not None
                             and exp_p > imp_p),
    }
    return out


def battery_eta_for(snap: dict) -> dict | None:
    """Time to reserve/full for a live Sunsynk snapshot (quick win 2)."""
    import insights
    return insights.battery_eta(
        soc_pct=snap.get("soc_pct"), battery_w=snap.get("battery_w"),
        nameplate_kwh=insights.effective_capacity_kwh(),
        reserve_pct=config.get_number("battery_reserve_pct", 10), now=insights._now())


def _tariffs_data() -> dict:
    """Current tariff summary: Cosy import bands, gas + export rates, standing
    charges, and the Flexible counterfactual. Cached per London day via
    _cached (several live Octopus calls). Raises octopus.OctopusError on
    fetch failure."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    import octopus
    settings = config.Settings.load()
    if not settings.octopus_configured:
        return {"configured": False}
    today = sync.today()

    def p(v):
        return round(v, 3) if isinstance(v, (int, float)) else None

    now = datetime.now(ZoneInfo("Europe/London"))
    octo = octopus.OctopusClient(settings.octopus_api_key,
                                 settings.octopus_account_number)
    res = {"configured": True, "date": today, "region": "",
           "import_bands": {}, "gas_p": None, "export_p": None,
           "flexible_p": None, "standing_elec_p": None, "standing_gas_p": None}
    try:
        # resolve_meters/day_bounds inside the try — a failure here must still
        # close the client session.
        meters = sync.resolve_meters(settings, octo)
        res["region"] = meters.region
        pf, pt = octo.day_bounds(today)
        if meters.import_tariff_code:
            code = meters.import_tariff_code
            prod, _ = octopus.parse_tariff_code(code)
            rates = octo.get_unit_rates(prod, code, "electricity", pf, pt)
            day_rates = [r.get("value_inc_vat") for r in rates]
            # Banded the way THIS tariff bands (Cosy by clock, others by
            # price); it was Cosy hours for every tariff.
            for r in sorted(rates, key=lambda r: r.get("value_inc_vat") or 0):
                w = octopus.classify_window(octopus.to_london(r["valid_from"]),
                                            r.get("value_inc_vat"), day_rates, code)
                res["import_bands"].setdefault(w, p(r.get("value_inc_vat")))
            res["band_times"] = band_times(sync.today_date(), code)
            res["is_cosy"] = octopus.is_cosy_product(code)
            res["standing_elec_p"] = p(octo.get_standing_charge(
                prod, meters.import_tariff_code, "electricity"))
        if meters.export_tariff_code:
            prod, _ = octopus.parse_tariff_code(meters.export_tariff_code)
            er = octo.get_unit_rates(prod, meters.export_tariff_code,
                                     "electricity", pf, pt)
            cur = octopus.rate_for(now, er) or (er[0] if er else None)
            res["export_p"] = p(cur.get("value_inc_vat")) if cur else None
        if meters.gas_tariff_code:
            prod, _ = octopus.parse_tariff_code(meters.gas_tariff_code)
            gr = octo.get_unit_rates(prod, meters.gas_tariff_code, "gas", pf, pt)
            cur = octopus.rate_for(now, gr) or (gr[0] if gr else None)
            res["gas_p"] = p(cur.get("value_inc_vat")) if cur else None
            res["standing_gas_p"] = p(octo.get_standing_charge(
                prod, meters.gas_tariff_code, "gas"))
        if meters.region:
            res["flexible_p"] = p(octo.get_flexible_import_rate(meters.region))
    finally:
        octo.close()
    return res


def current_window(now) -> str:
    """cheap|day|peak for `now` on the ACTIVE tariff: Cosy by clock, any
    other tariff by the stored price of the current period within today's
    range (tips and the forecast panel used Cosy hours for every tariff)."""
    import octopus
    code = config.get_text("import_tariff_code") or None
    if code is None or octopus.is_cosy_product(code):
        return octopus.cosy_window(now)
    row = db.rate_at("electricity_import", now.isoformat())
    return octopus.classify_window(
        now, row["rate_p_per_kwh"] if row else None,
        db.rates_for_local_day("electricity_import", now.date()), code)


def band_times(day, code: str | None) -> dict[str, str]:
    """{band: "HH:MM–HH:MM · …"} for a local day — Cosy's fixed windows, or
    any other tariff's cheap/day/peak runs from its stored rate periods."""
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    import octopus
    lon = ZoneInfo("Europe/London")
    cosy = not code or octopus.is_cosy_product(code)
    periods = [] if cosy else db.tariff_periods_for_local_day("electricity_import", day)
    if not cosy and not periods:
        return {}
    day_rates = [p["value_inc_vat"] for p in periods]
    t = datetime.combine(day, datetime.min.time(), lon).timestamp()
    end = datetime.combine(day + timedelta(days=1), datetime.min.time(), lon).timestamp()
    runs: list[list[str]] = []
    while t < end:
        local = datetime.fromtimestamp(t, lon)
        if cosy:
            band = octopus.cosy_window(local)
        else:
            hit = octopus.rate_for(datetime.fromtimestamp(t, timezone.utc), periods)
            band = (octopus.classify_window(local, hit["value_inc_vat"], day_rates, code)
                    if hit else None)
        frm = local.strftime("%H:%M")
        to = "24:00" if t + 1800 >= end else datetime.fromtimestamp(t + 1800, lon).strftime("%H:%M")
        if band and runs and runs[-1][0] == band and runs[-1][2] == frm:
            runs[-1][2] = to
        elif band:
            runs.append([band, frm, to])
        t += 1800
    out: dict[str, list[str]] = {}
    for band, a, b in runs:
        out.setdefault(band, []).append(f"{a}–{b}")
    return {k: " · ".join(v) for k, v in out.items()}


def _indepth_snapshot() -> dict:
    settings = config.Settings.load()
    if not settings.sunsynk_configured:
        return {"configured": False}
    import sunsynk
    sc = sunsynk.SunsynkClient(settings)
    try:
        return {"configured": True, **sc.get_indepth()}
    finally:
        sc.close()


def _build_tips(fc: dict, now) -> list[str]:
    import octopus
    code = config.get_text("import_tariff_code") or None
    cosy = code is None or octopus.is_cosy_product(code)
    win = current_window(now)
    tips = []
    if cosy:
        if win == "peak":
            tips.append("⚡ Peak window now (16:00–19:00) — delay heavy appliances if you can.")
        elif win == "cheap":
            tips.append("✅ Cheap window now — good time for high-load appliances or grid-charging.")
        else:
            tips.append("Cheap windows: 04:00–07:00, 13:00–16:00, 22:00–24:00 · Peak: 16:00–19:00.")
    else:
        # Other tariffs: their own windows, from today's stored rate periods.
        times = band_times(now.date(), code)
        if win == "peak":
            tips.append("⚡ Dearest prices now — delay heavy appliances if you can.")
        elif win == "cheap":
            tips.append("✅ Cheap window now — good time for high-load appliances or grid-charging.")
        if times.get("cheap"):
            line = f"Cheapest today: {times['cheap']}"
            if times.get("peak"):
                line += f" · Dearest: {times['peak']}"
            tips.append(line + ".")
    tm = (fc or {}).get("tomorrow") or {}
    outlook = tm.get("outlook")
    if outlook == "low":
        tips.append("🌥️ Low solar forecast tomorrow — consider topping up the battery in tonight's cheap window.")
    elif outlook == "high":
        tips.append("☀️ Strong solar forecast tomorrow — you'll likely export; avoid grid-charging tonight.")
    elif outlook == "moderate":
        tips.append("⛅ Moderate solar forecast tomorrow.")
    return tips


def _forecast_data() -> dict:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    import solar_forecast
    import weather
    now = datetime.now(ZoneInfo("Europe/London"))
    fc = weather.get_forecast()
    try:
        kwh = solar_forecast.forecast_kwh(fc)
    except Exception:  # noqa: BLE001 — optional
        kwh = {}
    return {**fc, "kwh": kwh, "current_window": current_window(now),
            "tips": _build_tips(fc, now)}


def _plan_data() -> dict:
    import planner
    live = _cached("live", config.LIVE_CACHE_SECONDS, _live_snapshot)
    soc = (live.get("sunsynk") or {}).get("soc_pct")
    return planner.get_plan(soc_pct=soc, fetch_soc=False)


def _account_data() -> dict:
    """Octopus account balance + Octoplus saving sessions (Kraken GraphQL),
    plus a self-computed bill forecast from our own daily summaries
    (month-to-date net cost projected to month end)."""
    import calendar

    import octopus_graphql
    ov = octopus_graphql.get_account_overview()

    today = sync.today_date()
    rows = [r for r in db.daily_summaries_range(today.replace(day=1).isoformat(),
                                                today.isoformat()) if _has_source(r)]
    costs = [r["net_cost_pence"] for r in rows if r["net_cost_pence"] is not None]
    # Today's row is PARTIAL (updated hourly) — averaging it in as a full day
    # drags the projection low every morning. Project from full days only.
    full = [r["net_cost_pence"] for r in rows
            if r["net_cost_pence"] is not None and r["date"] < today.isoformat()]
    forecast = None
    if costs:
        mtd = round(sum(costs), 2)
        days_in_month = calendar.monthrange(today.year, today.month)[1]
        if full:
            avg = sum(full) / len(full)
            projected = sum(full) + avg * (days_in_month - len(full))
        else:   # 1st of the month: only today's partial exists
            avg = mtd
            projected = mtd * days_in_month
        forecast = {
            "month": today.strftime("%Y-%m"),
            "month_to_date_pence": mtd,
            "days_with_data": len(costs),
            "days_in_month": days_in_month,
            "avg_daily_pence": round(avg, 2),
            "projected_month_pence": round(projected, 2),
        }
    return {**ov, "bill_forecast": forecast}


def _day_response(date: str):
    """Shared /day/<date> handler: canonicalise the date (fromisoformat also
    accepts '20261231', which would silently miss every stored row), build the
    payload, and let settled immutable days be browser-cached."""
    try:
        date = date_cls.fromisoformat(date).isoformat()
    except ValueError:
        return err("invalid date (expected YYYY-MM-DD)", 400)
    if not ("2000-01-01" <= date <= "2999-12-31"):
        return err("date out of range", 400)
    payload = _build_day_payload(date)
    resp = ok(payload)
    # Only a settled, priced day is immutable enough to cache; a failed or
    # empty past day used to be cached for an hour too.
    if date < sync.today() and payload["cost_status"] == "confirmed":
        resp.headers["Cache-Control"] = "private, max-age=3600"
    return resp


# ---------- history aggregation ----------

_MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _elec_cost(row) -> float | None:
    """Net electricity cost for a summary row: import + standing - export.
    None when the import cost isn't known."""
    if row["import_cost_pence"] is None:
        return None
    return round(row["import_cost_pence"] + (row["standing_elec_pence"] or 0)
                 - (row["export_earned_pence"] or 0), 2)


def _gas_cost(row) -> float | None:
    if row["gas_cost_pence"] is None and row["standing_gas_pence"] is None:
        return None
    return round((row["gas_cost_pence"] or 0) + (row["standing_gas_pence"] or 0), 2)


def _period_bounds(period: str, anchor: date_cls) -> tuple[date_cls, date_cls]:
    if period == "week":
        start = anchor - timedelta(days=anchor.weekday())   # Monday
        return start, start + timedelta(days=6)
    if period == "month":
        start = anchor.replace(day=1)
        nxt = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
        return start, nxt - timedelta(days=1)
    if period == "year":
        return anchor.replace(month=1, day=1), anchor.replace(month=12, day=31)
    raise ValueError("period must be week|month|year")


_BUCKET_KEYS = ("elec_cost", "gas_cost", "pv_kwh", "import_kwh", "export_kwh",
                "gas_kwh", "net_cost", "import_cost", "load_kwh", "self_kwh")


def _row_bucket(rows):
    """Sum summary rows into one cost/energy bucket. A key no row knows is
    None ("–"), never 0 — a gap or a future day is not a free, idle day."""
    rows = [r for r in rows if _has_source(r)]
    vals = {k: [] for k in _BUCKET_KEYS}
    for r in rows:
        vals["elec_cost"].append(_elec_cost(r))
        vals["gas_cost"].append(_gas_cost(r))
        vals["pv_kwh"].append(r["pv_kwh"])
        vals["import_kwh"].append(r["grid_import_kwh"])
        vals["export_kwh"].append(r["grid_export_kwh"])
        vals["gas_kwh"].append(r["gas_kwh"])
        vals["net_cost"].append(r["net_cost_pence"])
        vals["import_cost"].append(r["import_cost_pence"])
        vals["load_kwh"].append(r["house_load_kwh"])
        # Self-supplied energy, only where both sides are known.
        vals["self_kwh"].append(
            max(0.0, r["house_load_kwh"] - r["grid_import_kwh"])
            if r["house_load_kwh"] is not None and r["grid_import_kwh"] is not None
            else None)
    out = {}
    for k, vs in vals.items():
        known = [v for v in vs if v is not None]
        out[k] = round(sum(known), 2) if known else None
    out["days"] = len(rows)
    return out


def _total(buckets, key, nd=2):
    known = [b[key] for b in buckets if b[key] is not None]
    return round(sum(known), nd) if known else None


def _buckets_for(period: str, start: date_cls, end: date_cls):
    """(labels, buckets, table) for a period's range."""
    rows = db.daily_summaries_range(start.isoformat(), end.isoformat())
    by_date = {r["date"]: r for r in rows}
    labels, buckets, table = [], [], []
    if period == "year":
        for mi in range(12):
            month_rows = [r for r in rows if int(r["date"][5:7]) == mi + 1]
            labels.append(_MONTH_NAMES[mi])
            b = _row_bucket(month_rows)
            buckets.append(b)
            table.append({"label": _MONTH_NAMES[mi], **b})
    else:
        d = start
        while d <= end:
            ds = d.isoformat()
            r = by_date.get(ds)
            b = _row_bucket([r] if r else [])
            lab = _DAY_NAMES[d.weekday()] if period == "week" else str(d.day)
            labels.append(lab)
            buckets.append(b)
            table.append({"label": lab, "date": ds, **b})
            d += timedelta(days=1)
    for b in buckets:
        b["self_sufficiency"] = (round(b["self_kwh"] / b["load_kwh"] * 100, 1)
                                 if b["load_kwh"] and b["self_kwh"] is not None else None)
    return labels, buckets, table, rows


def _year_ago(d: date_cls) -> date_cls:
    try:
        return d.replace(year=d.year - 1)
    except ValueError:                     # 29 Feb
        return d.replace(year=d.year - 1, day=28)


def _totals(buckets) -> dict:
    t = {
        "elec_cost": _total(buckets, "elec_cost"),
        "gas_cost": _total(buckets, "gas_cost"),
        "net_cost": _total(buckets, "net_cost"),
        "pv_kwh": _total(buckets, "pv_kwh", 1),
        "import_kwh": _total(buckets, "import_kwh", 1),
        "export_kwh": _total(buckets, "export_kwh", 1),
        "gas_kwh": _total(buckets, "gas_kwh", 1),
        "load_kwh": _total(buckets, "load_kwh", 1),
        "self_kwh": _total(buckets, "self_kwh", 1),
        "days": sum(b["days"] for b in buckets),
    }
    load, self_kwh = t["load_kwh"], t["self_kwh"]
    t["self_sufficiency_pct"] = (round(self_kwh / load * 100, 1)
                                 if load and self_kwh is not None else None)
    return t


def _build_history_payload(period: str, anchor: date_cls) -> dict:
    start, end = _period_bounds(period, anchor)
    labels, buckets, table, rows = _buckets_for(period, start, end)
    totals = _totals(buckets)
    # Estimated saving vs Flexible Octopus over the period (unshifted load):
    # flex_rate * import_kwh - actual import cost, over days whose import is
    # both measured AND priced (an unpriced day is not a saving).
    flex = db.latest_tariff_rate("electricity_flexible")
    priced = [r for r in rows if _has_source(r) and r["import_cost_pence"] is not None
              and r["grid_import_kwh"] is not None]
    totals["flex_rate_p"] = flex
    totals["saving_pence"] = (round(sum(flex * r["grid_import_kwh"]
                                        - r["import_cost_pence"] for r in priced), 2)
                              if flex and priced else None)
    # The same period a year earlier, aligned bucket-by-bucket (padded with
    # None where the periods differ in length, e.g. a leap February).
    pstart, pend = _period_bounds(period, _year_ago(anchor))
    _, prev, _, _ = _buckets_for(period, pstart, pend)
    n = len(buckets)
    prev = (prev + [dict.fromkeys(prev[0] if prev else buckets[0])] * n)[:n]

    def series(bs, key):
        return [b.get(key) for b in bs]
    return {
        "period": period, "anchor": anchor.isoformat(),
        "range": {"start": start.isoformat(), "end": end.isoformat()},
        "labels": labels,
        "cost": {"elec": series(buckets, "elec_cost"), "gas": series(buckets, "gas_cost"),
                 "net": series(buckets, "net_cost")},
        "energy": {"pv": series(buckets, "pv_kwh"), "import": series(buckets, "import_kwh"),
                   "export": series(buckets, "export_kwh"), "gas": series(buckets, "gas_kwh"),
                   "load": series(buckets, "load_kwh"), "self": series(buckets, "self_kwh")},
        "self_sufficiency": series(buckets, "self_sufficiency"),
        "table": table, "totals": totals,
        "compare": {
            "range": {"start": pstart.isoformat(), "end": pend.isoformat()},
            "totals": _totals([b for b in prev if b.get("days") is not None]),
            "cost": {"net": series(prev, "net_cost"), "elec": series(prev, "elec_cost"),
                     "gas": series(prev, "gas_cost")},
            "energy": {"pv": series(prev, "pv_kwh"), "import": series(prev, "import_kwh"),
                       "export": series(prev, "export_kwh")},
            "self_sufficiency": series(prev, "self_sufficiency"),
        },
    }


def _calendar_data(year: int) -> list[dict]:
    """One entry per day of `year` for the History heatmap (None = no data)."""
    start, end = date_cls(year, 1, 1), date_cls(year, 12, 31)
    by_date = {r["date"]: r for r in db.daily_summaries_range(start.isoformat(),
                                                               end.isoformat())}
    out = []
    d = start
    while d <= end:
        r = by_date.get(d.isoformat())
        b = _row_bucket([r] if r else [])
        out.append({"date": d.isoformat(), "net_cost": b["net_cost"], "pv_kwh": b["pv_kwh"],
                    "import_kwh": b["import_kwh"], "export_kwh": b["export_kwh"],
                    "gas_kwh": b["gas_kwh"],
                    "self_sufficiency": (round(b["self_kwh"] / b["load_kwh"] * 100, 1)
                                         if b["load_kwh"] and b["self_kwh"] is not None
                                         else None)})
        d += timedelta(days=1)
    return out


def _reconcile_data(months: int = 6) -> dict:
    """Per-month: our computed net cost vs the Octopus statement total (when the
    statement fetch succeeds). The statement side is best-effort; our side is
    always present, so the table doubles as a manual bill cross-check."""
    import calendar

    import octopus_graphql
    statements = octopus_graphql.get_latest_statements()
    # Each bill is compared with OUR cost over the bill's own period. Bills
    # used to be keyed by their end month and set against a calendar month
    # (a 14 Sep–13 Oct bill was compared with all of October).
    by_statement = []
    for s in statements:
        try:
            frm = date_cls.fromisoformat((s.get("from") or "")[:10])
            to = date_cls.fromisoformat((s.get("to") or "")[:10]) if s.get("to") \
                else sync.today_date()
        except ValueError:
            continue
        b = _row_bucket(db.daily_summaries_range(frm.isoformat(), to.isoformat()))
        billed = s.get("total_pence")
        by_statement.append({
            "from": frm.isoformat(), "to": to.isoformat(),
            "days": (to - frm).days + 1, "billed_pence": billed,
            "our_cost_pence": b["net_cost"], "days_with_data": b["days"],
            "delta_pence": (round(b["net_cost"] - billed, 2)
                            if billed is not None and b["net_cost"] is not None else None),
        })

    cur = sync.today_date().replace(day=1)
    out = []
    for _ in range(months):
        y, m = cur.year, cur.month
        start = cur.isoformat()
        end = date_cls(y, m, calendar.monthrange(y, m)[1]).isoformat()
        rows = db.daily_summaries_range(start, end)
        b = _row_bucket(rows)
        out.append({
            "month": f"{y:04d}-{m:02d}",
            "our_cost_pence": b["net_cost"],
            "elec_pence": b["elec_cost"], "gas_pence": b["gas_cost"],
            "billed_pence": None, "delta_pence": None,
            "days_with_data": b["days"],
        })
        cur = (cur - timedelta(days=1)).replace(day=1)
    return {"months": out, "statements": by_statement,
            "billed_available": bool(by_statement)}


# ---------- data coverage (quick win 4) ----------

def _today():
    return sync.today_date()


_COVERAGE_TABLES = {"sunsynk": "solar_intervals", "elec": "electricity_intervals",
                    "export": "export_intervals", "gas": "gas_intervals"}


def _coverage_sources() -> list[str]:
    """The sources this install is expected to have (cache-only meter lookup)."""
    s = config.Settings.load()
    out = ["sunsynk"] if s.sunsynk_configured else []
    if s.octopus_configured:
        m = sync.resolve_meters(s)
        out += [name for name, ok in (("elec", m.import_mpan), ("export", m.export_mpan),
                                      ("gas", m.mprn)) if ok]
    return out


def _coverage_data(days: int = 90) -> dict:
    """Per day, the stored half-hours per source and an overall status:
    complete (every expected source has the whole day), partial, missing."""
    end = _today()
    start = end - timedelta(days=days)
    counts = {k: {} for k in _COVERAGE_TABLES}
    with db.conn() as c:
        for key, table in _COVERAGE_TABLES.items():
            for r in c.execute(
                    f"SELECT substr(interval_start, 1, 10) AS d, COUNT(*) AS n FROM {table} "  # noqa: S608  # nosec B608
                    "WHERE interval_start >= ? AND interval_start < ? GROUP BY d",
                    (start.isoformat(), end.isoformat())):
                counts[key][r["d"]] = r["n"]
    # No source configured (or none readable): judge by what is stored.
    sources = _coverage_sources() or [k for k in _COVERAGE_TABLES if counts[k]]
    out = []
    d = start
    while d < end:
        ds = d.isoformat()
        exp = sync.expected_intervals(ds)
        row = {"date": ds, "expected": exp, **{k: counts[k].get(ds, 0) for k in _COVERAGE_TABLES}}
        have = [row[s] for s in sources]
        row["status"] = ("complete" if have and all(n >= exp for n in have)
                         else "partial" if any(row[k] for k in _COVERAGE_TABLES)
                         else "missing")
        out.append(row)
        d += timedelta(days=1)
    return {"sources": sources, "days": out}

