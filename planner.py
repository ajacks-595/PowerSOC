"""Battery charge/dispatch planner.

Combines tomorrow's solar outlook (weather), grid carbon intensity (the
greenest upcoming window), the live battery SoC and the configured battery
capacity into one concrete, explainable overnight recommendation: whether to
grid-charge tonight and to what level.

`build_plan` is pure (unit-tested); `get_plan` gathers the live inputs and
fails soft, returning {"available": False} when nothing useful is known.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

log = logging.getLogger("powersoc.planner")
LONDON = ZoneInfo("Europe/London")

# Overnight target state-of-charge by tomorrow's solar outlook. Low solar ->
# fill from the cheap grid window; strong solar -> leave room for PV.
_TARGET_BY_OUTLOOK = {"low": 100, "moderate": 70, "high": 0, "unknown": 80}
# The usual Cosy grid-charge slots (pre-dawn is the cheapest, longest one).
_COSY_CHEAP_TEXT = "tonight's cheap window (22:00–24:00 / 04:00–07:00)"
_GENERIC_CHEAP_TEXT = "tonight's cheapest window"
# AC->stored charge efficiency: grid kWh drawn ≈ stored kWh / efficiency, so
# kWh and cost estimates don't silently understate by ~6%.
_CHARGE_EFFICIENCY = 0.94


def _now() -> datetime:
    return datetime.now(LONDON)


# Before this local hour the live SoC says nothing about tonight: the 06:25
# brief runs right after the 04–07 cheap charge (~100%), so "already at
# target -> hold" was the answer every single morning.
_PROJECT_BEFORE_HOUR = 18


def typical_evening_soc(days: int = 7, at: str = "22:00") -> float | None:
    """Median battery SoC at `at` (local) over the last `days` full days —
    the expected starting point for tonight's charge. None without data."""
    import statistics
    from datetime import timedelta

    import database as db
    vals = []
    today = _now().date()
    for back in range(1, days + 1):
        day = (today - timedelta(days=back)).isoformat()
        rows = [r for r in db.intervals_for_date("solar_intervals", day)
                if r["soc_pct"] is not None and r["interval_start"][11:16] <= at]
        if rows:
            vals.append(rows[-1]["soc_pct"])
    return round(statistics.median(vals), 1) if len(vals) >= 3 else None


def build_plan(*, tomorrow_outlook: str | None, current_soc_pct: float | None,
               nameplate_kwh: float | None, cheap_rate_p: float | None = None,
               charge_window_text: str = _COSY_CHEAP_TEXT,
               greenest: dict | None = None,
               soc_projected: bool = False) -> dict:
    """Pure planning logic → a recommendation dict (see keys below)."""
    outlook = (tomorrow_outlook or "unknown").lower()
    if outlook not in _TARGET_BY_OUTLOOK:
        outlook = "unknown"
    target = _TARGET_BY_OUTLOOK[outlook]
    soc = current_soc_pct
    reasons: list[str] = []

    if outlook == "high":
        action = "no_charge"
        target = 0
        headline = ("Don't grid-charge tonight — strong solar tomorrow should "
                    "refill the battery (and you'll likely export).")
        reasons.append("Strong solar forecast tomorrow: PV should recharge the battery.")
    elif soc is not None and soc >= target:
        action = "hold"
        if soc_projected:
            headline = (f"Battery typically ~{round(soc)}% by tonight — no "
                        "overnight charge needed.")
        else:
            headline = f"Battery already at {round(soc)}% — no overnight charge needed."
        reasons.append(
            f"{'Typical evening' if soc_projected else 'Current'} SoC {round(soc)}% "
            f"already meets the {target}% target for a {outlook}-solar day.")
    else:
        action = "grid_charge"
        headline = f"Charge to {target}% in {charge_window_text}."
        if outlook == "low":
            reasons.append("Low solar forecast tomorrow: top up from the grid in the cheap window.")
        elif outlook == "moderate":
            reasons.append("Moderate solar tomorrow: a partial top-up hedges a dull morning.")
        else:
            reasons.append("Solar outlook unknown: a cheap-window top-up is the safe default.")

    charge_kwh = est_cost_p = None
    if action == "grid_charge" and nameplate_kwh and soc is not None:
        charge_kwh = round(max(0.0, (target - soc) / 100.0 * nameplate_kwh)
                           / _CHARGE_EFFICIENCY, 2)
        if cheap_rate_p is not None:
            est_cost_p = round(charge_kwh * cheap_rate_p, 1)

    # Only when actually recommending a charge — a no_charge plan ending with
    # "cleanest time to charge" is a contradictory explanation.
    if action == "grid_charge" and greenest and greenest.get("from"):
        try:
            t = (datetime.fromisoformat(greenest["from"].replace("Z", "+00:00"))
                 .astimezone(LONDON).strftime("%H:%M"))
            reasons.append(
                f"Greenest grid ~{t} ({round(greenest['g'])} gCO₂/kWh) — cleanest time to charge.")
        except (ValueError, TypeError, KeyError):
            pass

    return {
        "available": True,
        "tomorrow_outlook": outlook,
        "current_soc_pct": round(soc) if soc is not None else None,
        "target_soc_pct": target,
        "recommended_action": action,        # grid_charge | hold | no_charge
        "headline": headline,
        "charge_kwh": charge_kwh,
        "est_cost_pence": est_cost_p,
        "charge_window": charge_window_text,
        "greenest": greenest,
        "reasons": reasons,
    }


def get_plan(soc_pct: float | None = None, fetch_soc: bool = True) -> dict:
    """Gather live inputs and build the plan. Fails soft.

    Pass `soc_pct` (and fetch_soc=False) to reuse an already-cached live
    snapshot instead of making a fresh Sunsynk call (the dashboard does this)."""
    import config

    outlook = None
    try:
        import weather
        outlook = ((weather.get_forecast() or {}).get("tomorrow") or {}).get("outlook")
    except Exception:  # noqa: BLE001, S110 — best-effort
        pass

    if soc_pct is None and fetch_soc:
        try:
            settings = config.Settings.load()
            if settings.sunsynk_configured:
                import sunsynk
                sc = sunsynk.SunsynkClient(settings)
                try:
                    soc_pct = sc.get_realtime().get("soc_pct")
                finally:
                    sc.close()
        except Exception:  # noqa: BLE001, S110 — best-effort
            pass

    greenest = None
    try:
        import carbon
        greenest = (carbon.get_intensity() or {}).get("greenest")
    except Exception:  # noqa: BLE001, S110 — best-effort
        pass

    cheap_rate = None
    try:
        import sync
        cheap_rate = sync._latest_window_rates().get("cheap")
    except Exception:  # noqa: BLE001, S110 — best-effort
        pass

    # Cosy window text only when the active tariff IS Cosy (persisted by the
    # daily sync); other tariffs get neutral wording instead of fixed hours
    # that don't exist on them.
    window_text = _COSY_CHEAP_TEXT
    try:
        import octopus
        code = config.get_text("import_tariff_code")
        if code and not octopus.is_cosy_product(code):
            window_text = _GENERIC_CHEAP_TEXT
    except Exception:  # noqa: BLE001, S110 — best-effort
        pass

    # v2 first: a simulation of the next 30 h when the inputs exist.
    try:
        v2 = plan_v2(soc_pct)
    except Exception:  # noqa: BLE001 — fall back to the heuristic
        log.exception("Charge simulation failed")
        v2 = None
    if v2:
        v2.update(tomorrow_outlook=(outlook or "unknown"), greenest=greenest)
        return v2

    nameplate = config.get_number("battery_nameplate_kwh")
    projected = False
    if _now().hour < _PROJECT_BEFORE_HOUR:
        try:
            evening = typical_evening_soc()
        except Exception:  # noqa: BLE001 — best-effort
            evening = None
        if evening is not None:
            soc_pct, projected = evening, True
    plan = build_plan(tomorrow_outlook=outlook, current_soc_pct=soc_pct,
                      nameplate_kwh=nameplate, cheap_rate_p=cheap_rate,
                      charge_window_text=window_text, greenest=greenest,
                      soc_projected=projected)
    plan["method"] = "heuristic"
    if projected:
        plan["soc_projected"] = True
        plan["reasons"].insert(0, f"Projected ~{round(soc_pct)}% at 22:00 — the typical "
                                  "evening level over the last week.")
    return plan


# ---------- v2: cost-minimising simulation (feature 13) ------------------------

def _simulate(load, pv, import_p, export_p, charge, *, soc_kwh, cap, reserve_kwh,
              eff=_CHARGE_EFFICIENCY):
    """Step the battery through the horizon. `charge[i]` = grid kWh drawn to
    charge in slot i. While grid-charging the house runs from the grid (no
    discharge in the same half-hour). Returns (cost_p, socs, imports) — socs
    being the state of charge at the START of each slot."""
    known = [p for p in import_p if p is not None]
    worst = max(known) if known else 0.0          # unknown price: assume the worst
    cost, socs, imports = 0.0, [], []
    soc = soc_kwh
    for i in range(len(load)):
        socs.append(soc)
        g = charge[i]
        stored = min(g * eff, cap - soc)
        g = stored / eff if eff else 0.0
        soc += stored
        net = load[i] - pv[i]
        imp, exp = g, 0.0
        if net > 0:
            use = 0.0 if g > 0 else min(net, max(0.0, soc - reserve_kwh))
            soc -= use
            imp += net - use
        else:
            chg = min(-net * eff, cap - soc)
            soc += chg
            exp = -net - chg / eff
        p_imp = import_p[i] if import_p[i] is not None else worst
        cost += imp * p_imp - exp * (export_p[i] or 0.0)
        imports.append(imp)
    return cost, socs, imports


def simulate_plan(*, slots, soc_pct, nameplate_kwh, reserve_pct, charge_kw, load, pv,
                  import_p, export_p) -> dict:
    """Pure: add grid charging in the cheapest known-price half-hours, a step
    at a time, only while each step lowers the total cost of the horizon
    (imports − exports, at the slots' own prices). Rate-limited by
    `charge_kw`, capped by capacity; never below the reserve."""
    n = len(slots)
    cap = float(nameplate_kwh)
    reserve = cap * (reserve_pct or 0) / 100
    soc0 = cap * soc_pct / 100
    kw = dict(soc_kwh=soc0, cap=cap, reserve_kwh=reserve)
    max_slot = charge_kw * 0.5
    step = max(0.1, max_slot / 2)
    charge = [0.0] * n
    base_cost, base_socs, _ = _simulate(load, pv, import_p, export_p, charge, **kw)
    best = base_cost
    order = sorted((i for i in range(n) if import_p[i] is not None), key=lambda i: import_p[i])
    improved = True
    while improved:
        improved = False
        for i in order:
            if charge[i] >= max_slot - 1e-9:
                continue
            inc = min(step, max_slot - charge[i])
            charge[i] += inc
            cost = _simulate(load, pv, import_p, export_p, charge, **kw)[0]
            if cost < best - 0.05:            # at least 0.05p better
                best, improved = cost, True
                break
            charge[i] -= inc
    cost, socs, _ = _simulate(load, pv, import_p, export_p, charge, **kw)
    lbl = [t.strftime("%H:%M") for t in slots]
    idx = [i for i in range(n) if charge[i] > 1e-6]
    charge_kwh = round(sum(charge), 2)
    charge_cost = round(sum(charge[i] * import_p[i] for i in idx), 1)
    pct = [round(s / cap * 100, 1) for s in socs]
    base_pct = [round(s / cap * 100, 1) for s in base_socs]
    low = min(range(n), key=lambda i: base_pct[i])
    reasons = [f"Without charging the battery bottoms out at {base_pct[low]:.0f}% at "
               f"{lbl[low]}; typical use over the next {n // 2} h is ~{sum(load):.0f} kWh "
               f"against ~{sum(pv):.0f} kWh of solar."]
    runs: list[list[int]] = []
    for i in idx:   # charge slots -> windows; a pause of up to an hour at the
        # same price (battery briefly full) doesn't split a window
        if runs and i - runs[-1][-1] <= 3 and import_p[i] == import_p[runs[-1][-1]]:
            runs[-1].append(i)
        else:
            runs.append([i])
    day0 = slots[0].date() if slots else None
    windows = ", ".join(
        ("tomorrow " if slots[r[0]].date() != day0 else "")
        + f"{lbl[r[0]]}–{(slots[r[-1]] + timedelta(minutes=30)):%H:%M}" for r in runs)
    if idx:
        prices = sorted({import_p[i] for i in idx})
        action = "grid_charge"
        headline = (f"Grid-charge ~{charge_kwh:.1f} kWh in {windows} "
                    f"(≈£{charge_cost / 100:.2f}) — saves ≈£{(base_cost - cost) / 100:.2f} "
                    "vs not charging.")
        reasons.append(f"Charges only in the cheapest half-hours ({', '.join(f'{p:g}p' for p in prices)}), "
                       f"at most {charge_kw:g} kW, stopping where another kWh no longer pays.")
        target = pct[idx[-1] + 1] if idx[-1] + 1 < n else pct[idx[-1]]
    else:
        action = "hold" if base_pct[low] > (reserve_pct or 0) + 0.5 else "no_charge"
        headline = ("No grid charge needed — battery and solar should cover the next "
                    f"{n // 2} hours." if action == "hold" else
                    "Don't grid-charge — the cheap rate doesn't beat the price it would avoid.")
        target = None
    return {
        "available": True, "method": "simulated", "recommended_action": action,
        "headline": headline, "charge_kwh": charge_kwh,
        "est_cost_pence": charge_cost if idx else None,
        "saving_pence": round(base_cost - cost, 1),
        "charge_slots": [{"label": lbl[i], "from": slots[i].isoformat(),
                          "kwh": round(charge[i], 2), "price": import_p[i]} for i in idx],
        "target_soc_pct": target, "current_soc_pct": round(soc_pct),
        "charge_window": windows or None,
        "trajectory": [{"label": lbl[i], "from": slots[i].isoformat(), "soc": pct[i],
                        "soc_baseline": base_pct[i], "import_p": import_p[i]} for i in range(n)],
        "reasons": reasons,
    }


def is_dynamic_tariff(code: str) -> bool:
    c = (code or "").upper()
    return "AGILE" in c or "TRACKER" in c


def stored_price(fuel: str, t: datetime) -> float | None:
    """The stored tariff period's price at an instant (None if not stored)."""
    from datetime import timezone

    import database as db
    import octopus
    periods = db.tariff_periods_for_local_day(fuel, t.astimezone(LONDON).date())
    hit = octopus.rate_for(t.astimezone(timezone.utc), periods) if periods else None
    return hit["value_inc_vat"] if hit else None


def fill_ahead(slots, prices, code: str, lookup=None) -> list:
    """Fixed-window tariffs (Cosy, Go, Flux, flat) publish their periods only
    to the next midnight: assume an unknown half-hour costs what the same
    time did 24 h earlier (`lookup(t)` gives the stored price at an instant).
    Never for Agile/Tracker, whose prices change every day."""
    if is_dynamic_tariff(code):
        return list(prices)
    lookup = lookup or (lambda t: stored_price("electricity_import", t))
    out = list(prices)
    for i, t in enumerate(slots):
        if out[i] is None:
            # 24 h of real time earlier, then that day's same wall-clock slot
            out[i] = lookup(datetime.fromtimestamp(t.timestamp() - 86400, LONDON))
    return out


def _profiles(days: int = 14) -> tuple[dict, dict] | None:
    """Median house load and PV per local half-hour over recent days."""
    import statistics
    from collections import defaultdict
    from datetime import timedelta as td

    import database as db
    end = _now().date()
    load, pv, seen = defaultdict(list), defaultdict(list), set()
    for r in db.intervals_between("solar_intervals", (end - td(days=days)).isoformat(),
                                  end.isoformat()):
        k = r["interval_start"][11:16]
        if r["house_load_kwh"] is not None:
            load[k].append(r["house_load_kwh"])
            seen.add(r["interval_start"][:10])
        if r["pv_kwh"] is not None:
            pv[k].append(r["pv_kwh"])
    if len(seen) < 7:
        return None
    return ({k: statistics.median(v) for k, v in load.items()},
            {k: statistics.median(v) for k, v in pv.items()})


def plan_v2(soc_pct: float | None, horizon: int = 60) -> dict | None:
    """Gather the inputs for simulate_plan; None when they aren't there."""
    from datetime import timedelta as td
    from datetime import timezone

    import config
    import database as db
    import insights
    import octopus
    nameplate = insights.effective_capacity_kwh()
    prof = _profiles()
    if soc_pct is None or not nameplate or prof is None:
        return None
    load_p, pv_p = prof
    now = _now()
    start = now.replace(minute=0 if now.minute < 30 else 30, second=0, microsecond=0)
    slots = [datetime.fromtimestamp(start.timestamp() + 1800 * i, LONDON) for i in range(horizon)]
    periods = {d: (db.tariff_periods_for_local_day("electricity_import", d),
                   db.tariff_periods_for_local_day("electricity_export", d))
               for d in {t.date() for t in slots}}

    def price(t, k):
        ps = periods[t.date()][k]
        hit = octopus.rate_for(t.astimezone(timezone.utc), ps) if ps else None
        return hit["value_inc_vat"] if hit else None
    imp = [price(t, 0) for t in slots]
    exp = [price(t, 1) for t in slots]
    imp = fill_ahead(slots, imp, config.get_text("import_tariff_code") or "")
    if sum(p is not None for p in imp) < horizon / 2:
        return None
    # Scale the typical PV shape to the calibrated forecast for each day.
    scale = {}
    try:
        import solar_forecast
        import weather
        f = solar_forecast.forecast_kwh(weather.get_forecast())
        prof_day = sum(pv_p.values()) or 0
        for d, kwh in ((now.date(), f.get("today_kwh")),
                       (now.date() + td(days=1), f.get("tomorrow_kwh"))):
            if kwh is not None and prof_day > 0:
                scale[d] = kwh / prof_day
    except Exception:  # noqa: BLE001, S110 — the typical profile is the fallback
        pass
    key = lambda t: t.strftime("%H:%M")  # noqa: E731
    load = [load_p.get(key(t), 0.0) for t in slots]
    pv = [pv_p.get(key(t), 0.0) * scale.get(t.date(), 1.0) for t in slots]
    return simulate_plan(
        slots=slots, soc_pct=soc_pct, nameplate_kwh=nameplate,
        reserve_pct=config.get_number("battery_reserve_pct", 10),
        charge_kw=config.get_number("battery_charge_kw") or 3.0,
        load=load, pv=pv, import_p=imp, export_p=exp)

