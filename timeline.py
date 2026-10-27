"""The next 24 hours, half-hour by half-hour: import/export price (from the
stored tariff periods), the tariff's band, and the grid's carbon intensity —
plus the cheapest and greenest 3-hour windows (quick win 7). Also served to
token consumers at /api/home/timeline (a heating controller can schedule
on it). Unknown prices stay None, never 0."""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import config
import database as db
import octopus

LONDON = ZoneInfo("Europe/London")
SLOTS = 48


def _window(slots: list[dict], key: str, n: int = 6) -> dict | None:
    """Lowest-average run of n contiguous slots with a known `key`."""
    best = None
    for i in range(len(slots) - n + 1):
        vals = [s[key] for s in slots[i:i + n]]
        if any(v is None for v in vals):
            continue
        avg = sum(vals) / n
        if best is None or avg < best[0]:
            best = (avg, i)
    if best is None:
        return None
    avg, i = best
    return {"from": slots[i]["from"], "from_label": slots[i]["label"],
            "to_label": slots[i + n - 1]["to_label"], "avg": round(avg, 2),
            "avg_p" if key == "import_p" else "avg_g": round(avg, 2)}


def build(now: datetime | None = None) -> dict:
    now = (now or datetime.now(LONDON)).astimezone(LONDON)
    start = now.replace(minute=0 if now.minute < 30 else 30, second=0, microsecond=0)
    t0 = start.timestamp()
    code = config.get_text("import_tariff_code") or None
    cosy = code is None or octopus.is_cosy_product(code)
    days = sorted({datetime.fromtimestamp(t0 + 1800 * i, LONDON).date() for i in range(SLOTS)})
    imp = {d: db.tariff_periods_for_local_day("electricity_import", d) for d in days}
    exp = {d: db.tariff_periods_for_local_day("electricity_export", d) for d in days}
    carbon_g: dict[str, float] = {}
    try:
        import carbon
        for s in (carbon.get_intensity() or {}).get("slots") or []:
            if s.get("from") and s.get("g") is not None:
                carbon_g[s["from"]] = s["g"]
    except Exception:  # noqa: BLE001, S110 — carbon is optional
        pass
    slots = []
    for i in range(SLOTS):
        t = t0 + 1800 * i
        utc = datetime.fromtimestamp(t, timezone.utc)
        local = utc.astimezone(LONDON)
        d = local.date()
        hit = octopus.rate_for(utc, imp[d]) if imp[d] else None
        p = hit["value_inc_vat"] if hit else None
        xhit = octopus.rate_for(utc, exp[d]) if exp[d] else None
        if cosy:
            band = octopus.cosy_window(local)
        else:
            band = (octopus.classify_window(local, p, [r["value_inc_vat"] for r in imp[d]], code)
                    if p is not None else None)
        slots.append({
            "from": utc.isoformat(), "label": local.strftime("%H:%M"),
            "to_label": datetime.fromtimestamp(t + 1800, LONDON).strftime("%H:%M"),
            "import_p": p, "export_p": xhit["value_inc_vat"] if xhit else None,
            "band": band, "carbon_g": carbon_g.get(utc.strftime("%Y-%m-%dT%H:%MZ"))})
    # Fixed-window tariffs publish only to the next midnight: fill the rest
    # from the same time a day earlier, marked "assumed".
    import planner
    if code and not planner.is_dynamic_tariff(code):
        for s in slots:
            if s["import_p"] is None:
                t = datetime.fromisoformat(s["from"]).timestamp() - 86400
                prev = datetime.fromtimestamp(t, LONDON)
                p = planner.stored_price("electricity_import", prev)
                if p is not None:
                    local = datetime.fromtimestamp(t + 86400, LONDON)
                    s["import_p"], s["assumed"] = p, True
                    s["band"] = (octopus.cosy_window(local) if cosy else octopus.classify_window(
                        local, p, [r["value_inc_vat"] for r in imp[prev.date()]]
                        if prev.date() in imp else [p], code))
                    if s["export_p"] is None:
                        s["export_p"] = planner.stored_price("electricity_export", prev)
    known = [s for s in slots if s["import_p"] is not None and not s.get("assumed")]
    return {"slots": slots, "tariff_code": code,
            "prices_known_until": known[-1]["to_label"] if known else None,
            "cheapest_3h": _window(slots, "import_p"),
            "greenest_3h": _window(slots, "carbon_g")}
