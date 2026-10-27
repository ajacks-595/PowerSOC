"""Data fetch / backfill orchestration.

`run_daily()` fetches yesterday's data from both upstreams, upserts intervals,
recomputes the daily summary, and logs each attempt. `run_backfill()` walks
history (Octopus in 90-day windows, Sunsynk day-by-day until empty) in a
background thread on first run.

Design principle (from the spec): no silent failures. Every upstream call is
wrapped so one source failing degrades the result rather than aborting the run;
each attempt writes a `sync_log` row.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import asdict
from datetime import date as date_cls
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

import config
import database as db
import octopus
import sunsynk

log = logging.getLogger("powersoc.sync")
LONDON = ZoneInfo("Europe/London")

# Discovered meter details are cached in settings under these keys so we don't
# re-hit the account endpoint every run.
_METER_CACHE_KEY = "octopus_meters"


# ---------- date helpers --------------------------------------------------

def _today_local() -> date_cls:
    return datetime.now(LONDON).date()


def today() -> str:
    return _today_local().isoformat()


def today_date() -> date_cls:
    """Today's date in Europe/London. Use instead of date.today(), which uses
    the host's local timezone and can be a day off near midnight."""
    return _today_local()


def yesterday() -> str:
    return (_today_local() - timedelta(days=1)).isoformat()


def _daterange(start: date_cls, end: date_cls):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


# ---------- pure derived metrics (tested in test_calculations) ------------

def self_sufficiency_pct(grid_import_kwh, house_load_kwh) -> float | None:
    """(1 - grid_import/house_load) * 100, clamped to [0, 100].

    Returns None when house load is unknown/zero (can't be computed)."""
    if not house_load_kwh or house_load_kwh <= 0:
        return None
    pct = (1 - (grid_import_kwh or 0) / house_load_kwh) * 100
    return max(0.0, min(100.0, round(pct, 1)))


def net_cost_pence(import_cost, standing_elec, gas_cost, standing_gas,
                   export_earned) -> float:
    """Combined net cost: import + standing(elec+gas) + gas - export credit."""
    return round((import_cost or 0) + (standing_elec or 0) + (gas_cost or 0)
                 + (standing_gas or 0) - (export_earned or 0), 4)


def saving_vs_flexible_pence(flex_rate_p, total_import_kwh, import_cost_pence
                             ) -> float | None:
    """Estimated import saving vs the Flexible Octopus flat rate (unshifted).
    None when either side is unknown — an unpriced day is not a saving of the
    whole Flexible cost."""
    if flex_rate_p is None or import_cost_pence is None or total_import_kwh is None:
        return None
    return round(flex_rate_p * (total_import_kwh or 0) - (import_cost_pence or 0), 4)


def _sum(rows, key):
    return round(sum((r[key] or 0) for r in rows), 4)


def _latest_export_rate() -> float | None:
    """Most recent known export unit rate (p/kWh) from stored export intervals —
    used to value the Sunsynk export estimate for an unsettled day."""
    with db.conn() as c:
        r = c.execute("SELECT rate_p_per_kwh FROM export_intervals "
                      "WHERE rate_p_per_kwh IS NOT NULL "
                      "ORDER BY interval_start DESC LIMIT 1").fetchone()
    return r[0] if r else None


# ---------- daily summary aggregation -------------------------------------

def expected_intervals(date: str) -> int:
    """Half-hour count of a local day: 48 normally, 46/50 on DST transitions.

    Compares epoch timestamps — subtracting two datetimes that share a tzinfo
    is WALL-CLOCK arithmetic in Python and would always yield 24h."""
    start = datetime.fromisoformat(date).replace(tzinfo=LONDON)
    end = start + timedelta(days=1)
    return int((end.timestamp() - start.timestamp()) // 1800)


def compute_daily_summary(date: str, *, standing_elec_p: float | None = None,
                          standing_gas_p: float | None = None) -> dict:
    """Aggregate stored intervals for `date` into a daily_summaries row dict.

    Reads solar/electricity/export/gas intervals from the DB; applies the
    supplied standing charges (p/day, added once). Computes self-sufficiency
    and net cost. Sets *_ok / data_partial / *_estimated flags.

    A value the app doesn't have is None, never 0 (the 2026-10-07 rule): no
    solar rows -> no PV figure; an unpriced interval -> no import cost; no
    import figure or cost -> no net cost. Rendering turns None into "–"."""
    # A re-run during a transient standing-charge fetch failure must not wipe
    # previously-stored charges (and silently understate net cost) — keep the
    # existing row's values when the caller has none.
    if standing_elec_p is None or standing_gas_p is None:
        prev = db.get_daily_summary(date)
        if prev is not None:
            if standing_elec_p is None:
                standing_elec_p = prev["standing_elec_pence"]
            if standing_gas_p is None:
                standing_gas_p = prev["standing_gas_pence"]

    solar = db.intervals_for_date("solar_intervals", date)
    elec = db.intervals_for_date("electricity_intervals", date)
    export = db.intervals_for_date("export_intervals", date)
    gas = db.intervals_for_date("gas_intervals", date)

    def priced_sum(rows, key):
        return None if any(r[key] is None for r in rows) else _sum(rows, key)

    house_load = _sum(solar, "house_load_kwh") if solar else None
    gas_kwh = _sum(gas, "consumption_kwh") if gas else None
    gas_cost = priced_sum(gas, "cost_pence") if gas else None

    # Import: Octopus is authoritative once settled (>=40 intervals). Until then
    # SMETS2 lag can run a full day late — but the Sunsynk CT measures the same
    # grid flow in real-time (within ~2% on our meter), so it is the estimate,
    # priced per half-hour from the stored tariff periods.
    ct = [r for r in solar if r["grid_import_kwh"] is not None]
    windows: dict[str, float | None] = {"cheap": 0.0, "day": 0.0, "peak": 0.0}
    import_estimated = 0
    if len(elec) >= 40 or (elec and not ct):
        grid_import = _sum(elec, "consumption_kwh")
        import_cost = priced_sum(elec, "cost_pence")
        for r in elec:
            if r["tariff_window"] in windows:
                windows[r["tariff_window"]] += r["consumption_kwh"] or 0
        # 40-47 settled half-hours: the rest used to count as 0 kWh. Fill the
        # ones the meter hasn't reported from the CT (flagged as estimated).
        metered = {r["interval_start"][11:16] for r in elec}
        gaps = [r for r in ct if r["interval_start"][11:16] not in metered]
        if len(elec) < expected_intervals(date) and gaps:
            fill, fill_cost = _ct_import_estimate(gaps, date)
            for w, kwh in fill.items():
                windows[w] += kwh
            grid_import = round(grid_import + sum(fill.values()), 4)
            import_cost = (None if import_cost is None or fill_cost is None
                           else round(import_cost + fill_cost, 4))
            if sum(fill.values()) > 0:
                import_estimated = 1
    elif ct:
        windows, import_cost = _ct_import_estimate(ct, date)
        grid_import = round(sum(windows.values()), 4)
        import_estimated = 1
    else:
        grid_import = import_cost = None
        windows = {"cheap": None, "day": None, "peak": None}

    # Export: Octopus is authoritative once the day is fully settled (>=40
    # intervals). Intraday it lags (only a few early intervals settle), so for
    # a partial day we use the Sunsynk CT estimate (which covers the whole day
    # so far) and flag it; the 06:25 master run reconciles once Octopus settles.
    sun_export = _sum(solar, "grid_export_kwh")
    export_unpriced = False
    if len(export) >= 40 or (export and not (solar and sun_export > 0)):
        grid_export = _sum(export, "export_kwh")
        export_earned = priced_sum(export, "earned_pence")
        export_unpriced = export_earned is None
        export_estimated = 0
    elif solar and sun_export > 0:
        grid_export = sun_export
        # Value the estimate at the latest known export rate so today's net cost
        # reflects export through the day; finalises when Octopus settles. No
        # known export tariff means nothing is being earned.
        er = _latest_export_rate()
        export_earned = round(sun_export * er, 2) if er else 0.0
        export_estimated = 1
    elif solar:
        grid_export, export_earned, export_estimated = 0.0, 0.0, 0
    else:
        grid_export = export_earned = None
        export_estimated = 0

    soc_vals = [r["soc_pct"] for r in solar if r["soc_pct"] is not None]
    peak_soc = max(soc_vals) if soc_vals else None
    end_soc = soc_vals[-1] if soc_vals else None

    net = None
    if (import_cost is not None and not export_unpriced
            and not (gas and gas_cost is None)):
        net = net_cost_pence(import_cost, standing_elec_p, gas_cost,
                             standing_gas_p, export_earned)

    def r4(v):
        return None if v is None else round(v, 4)

    summary = {
        "date": date,
        "pv_kwh": _sum(solar, "pv_kwh") if solar else None,
        "battery_charge_kwh": _sum(solar, "battery_charge_kwh") if solar else None,
        "battery_discharge_kwh": _sum(solar, "battery_discharge_kwh") if solar else None,
        "peak_soc_pct": peak_soc,
        "end_soc_pct": end_soc,
        "grid_import_kwh": grid_import,
        "grid_export_kwh": grid_export,
        "house_load_kwh": house_load,
        "cheap_import_kwh": r4(windows["cheap"]),
        "day_import_kwh": r4(windows["day"]),
        "peak_import_kwh": r4(windows["peak"]),
        "import_cost_pence": import_cost,
        "export_earned_pence": export_earned,
        "standing_elec_pence": standing_elec_p,
        "gas_kwh": gas_kwh,
        "gas_cost_pence": gas_cost,
        "standing_gas_pence": standing_gas_p,
        "self_sufficiency_pct": (self_sufficiency_pct(grid_import, house_load)
                                 if grid_import is not None else None),
        "net_cost_pence": net,
        "sunsynk_ok": 1 if solar else 0,
        "octopus_ok": 1 if elec else 0,
        # Partial = fewer half-hours than the day actually has (46/48/50,
        # DST-aware) so the catch-up keeps reconciling 40-47-interval days.
        # The >=40 threshold above still decides authoritative-vs-estimate.
        "data_partial": 1 if (elec and len(elec) < expected_intervals(date)) else 0,
        "export_estimated": export_estimated,
        "import_estimated": import_estimated,
    }
    return summary


def _parse_local(ts: str) -> datetime:
    """Parse a stored interval timestamp as local (Europe/London) time. Sunsynk
    /day returns inverter-local times; we treat them as London."""
    return datetime.fromisoformat(ts).replace(tzinfo=LONDON)


def _ct_import_estimate(rows, date: str) -> tuple[dict, float | None]:
    """Band + price the Sunsynk CT's half-hourly grid import for a day.

    Each half-hour is priced from the stored tariff period covering it
    (`tariff_rates`, refreshed daily) and banded the way the tariff bands it.
    It used to use Cosy clock windows and the latest per-window rate for
    every tariff — on Go/Agile that invented peak kWh (firing the high_peak
    alert) and mispriced by ~40%, and an unknown rate counted as 0p (a free
    day). Cosy keeps a fallback to the latest settled per-window rate; a rate
    still unknown makes the COST unknown (None), not zero."""
    code = config.get_text("import_tariff_code") or None
    cosy = code is None or octopus.is_cosy_product(code)
    fallback = _latest_window_rates() if cosy else {}
    periods = db.tariff_periods_for_local_day(
        "electricity_import", date_cls.fromisoformat(date))
    day_rates = [p["value_inc_vat"] for p in periods]
    windows = {"cheap": 0.0, "day": 0.0, "peak": 0.0}
    cost, unknown = 0.0, False
    for r in rows:
        ki = r["grid_import_kwh"] or 0
        if ki <= 0:
            continue
        ts = _parse_local(r["interval_start"])
        hit = octopus.rate_for(ts, periods) if periods else None
        p = hit["value_inc_vat"] if hit else fallback.get(octopus.cosy_window(ts))
        windows[octopus.classify_window(ts, p, day_rates, code)] += ki
        if p is None:
            unknown = True
        else:
            cost += ki * p
    return windows, (None if unknown else round(cost, 2))


def _latest_window_rates() -> dict[str, float]:
    """Latest per-Cosy-window import rate (p/kWh), derived from the most recent
    electricity_intervals row in each window. {} when nothing's been imported
    yet — the caller then treats the rate as unknown."""
    out: dict[str, float] = {}
    with db.conn() as c:
        for win in ("cheap", "day", "peak"):
            r = c.execute(
                "SELECT rate_p_per_kwh FROM electricity_intervals "
                "WHERE tariff_window=? AND rate_p_per_kwh IS NOT NULL "
                "ORDER BY interval_start DESC LIMIT 1", (win,)).fetchone()
            if r and r["rate_p_per_kwh"] is not None:
                out[win] = r["rate_p_per_kwh"]
    return out


# ---------- meter resolution ----------------------------------------------

_METER_CACHE_TTL = 24 * 3600


def _cached_discovery(account: str, max_age: float | None = _METER_CACHE_TTL
                      ) -> tuple[octopus.MeterInfo, float] | None:
    """(info, age_seconds) from the discovery cache, or None. max_age=None
    accepts any age (the fallback when re-discovery fails)."""
    raw = db.setting_get(_METER_CACHE_KEY)
    if not raw:
        return None
    try:
        d = json.loads(raw)
        age = time.time() - d.get("ts", 0)
        if d.get("account") != account or (max_age is not None and age > max_age):
            return None
        return octopus.MeterInfo(**d["info"]), age
    except (ValueError, TypeError, KeyError):
        return None


def _store_discovery(account: str, info: octopus.MeterInfo) -> None:
    db.setting_set(_METER_CACHE_KEY, json.dumps(
        {"account": account, "ts": time.time(), "info": asdict(info)}))


def clear_meter_cache() -> None:
    """Forget cached discovery (call when Octopus credentials change)."""
    db.setting_set(_METER_CACHE_KEY, None)


def resolve_meters(settings, octo: "octopus.OctopusClient | None" = None,
                   force_discover: bool = False) -> octopus.MeterInfo:
    """Return meter details: explicit settings first, blanks filled from a
    discovery result cached for 24h per account.

    Discovery used to run on EVERY run_daily (tariff codes are never in
    settings), and it probes each meter serial for recent consumption — 21
    Octopus GETs per run where ~8 carry data, 63 per intraday tick. The cache
    (`octopus_meters` setting, keyed by account number) makes it ~daily.

    When the cache has expired and re-discovery fails (an /accounts/ blip),
    the last discovery is used however old: carrying on WITHOUT a tariff code
    stored a day's import unpriced, marked it settled, and cleared the stored
    tariff code (2026-10 review, P1). With no octo client, any cached
    discovery is used (cache-only callers never trigger API calls)."""
    info = octopus.MeterInfo(
        import_mpan=settings.octopus_mpan,
        import_serial=settings.octopus_meter_serial,
        export_mpan=settings.octopus_export_mpan,
        export_serial=settings.octopus_export_serial,
        mprn=settings.octopus_mprn,
        gas_serial=settings.octopus_gas_serial,
    )
    account = settings.octopus_account_number
    hit = None if force_discover else _cached_discovery(account)
    discovered = hit[0] if hit else None
    if discovered is None and octo is not None:
        try:
            discovered = octo.discover_meters()
            _store_discovery(account, discovered)
        except octopus.OctopusError as e:
            stale = _cached_discovery(account, max_age=None)
            if stale:
                discovered = stale[0]
                log.warning("Meter discovery failed (%s) — using cached discovery "
                            "from %.0fh ago", e, stale[1] / 3600)
            else:
                log.warning("Meter discovery failed: %s", e)
    elif discovered is None:
        stale = _cached_discovery(account, max_age=None)
        discovered = stale[0] if stale else None
    if discovered is not None:
        # Explicit settings win; fill blanks from discovery.
        for k, v in asdict(discovered).items():
            if not getattr(info, k) and v:
                setattr(info, k, v)
    return info


def _octopus_sources(meters) -> list[tuple[str, str, str]]:
    """(sync_log source, interval table, agreement kind) per configured meter."""
    out = []
    if meters.import_mpan and meters.import_serial:
        out.append(("octopus_elec", "electricity_intervals", "import"))
    if meters.export_mpan and meters.export_serial:
        out.append(("octopus_export", "export_intervals", "export"))
    if meters.mprn and meters.gas_serial:
        out.append(("octopus_gas", "gas_intervals", "gas"))
    return out


def _octopus_gaps(date: str, meters, force: bool = False) -> set[str]:
    """Octopus sources that should be (re-)fetched for `date`: configured,
    in supply that day (an agreement covers it), and either short of the
    day's half-hours or holding an unpriced interval. Drives the backfill
    (fetch only what's missing) and the catch-up (re-sync until settled)."""
    need = set()
    for source, table, kind in _octopus_sources(meters):
        tariff = meters.tariff_on(kind, date)
        if not tariff and (meters.agreements or {}).get(kind):
            continue                      # not supplied by Octopus that day
        if force:
            need.add(source)
            continue
        rows = db.intervals_for_date(table, date)
        if (len(rows) < expected_intervals(date)
                or (tariff and any(r["rate_p_per_kwh"] is None for r in rows))):
            need.add(source)
    return need


# ---------- per-source day sync -------------------------------------------

def _rates_for_day(octo, tariff_code, fuel, date):
    if not tariff_code:
        return []
    try:
        product, _region = octopus.parse_tariff_code(tariff_code)
        pf, pt = octo.day_bounds(date)
        return octo.get_unit_rates(product, tariff_code, fuel, pf, pt)
    except octopus.OctopusError as e:
        log.warning("Rate fetch failed (%s %s): %s", fuel, date, e)
        return []


_MONEY_COLUMN = {"electricity_intervals": ("cost_pence", "consumption_kwh"),
                 "gas_intervals": ("cost_pence", "consumption_kwh"),
                 "export_intervals": ("earned_pence", "export_kwh")}


def _keep_stored_prices(table: str, date: str, intervals: list[dict]) -> int:
    """Fill unpriced fresh intervals from the price already stored for the
    same half-hour. A rate-fetch blip used to upsert NULL over a correctly
    priced day — and the day still looked settled, so nothing re-fetched it.
    Returns how many intervals are still unpriced afterwards."""
    money, qty = _MONEY_COLUMN[table]
    missing = [iv for iv in intervals if iv.get("rate_p_per_kwh") is None]
    if not missing:
        return 0
    stored = {r["interval_start"]: r for r in db.intervals_for_date(table, date)
              if r["rate_p_per_kwh"] is not None}
    still = 0
    for iv in missing:
        old = stored.get(iv["interval_start"])
        if old is None:
            still += 1
            continue
        iv["rate_p_per_kwh"] = old["rate_p_per_kwh"]
        iv[money] = round((iv.get(qty) or 0) * old["rate_p_per_kwh"], 4)
        if "tariff_window" in iv:
            iv["tariff_window"] = old["tariff_window"]
    return still


def sync_sunsynk_day(client: "sunsynk.SunsynkClient", date: str
                     ) -> tuple[int, "sunsynk.DayResult"]:
    """Fetch + store one Sunsynk day. Returns (intervals written, DayResult)
    so callers can tell a genuinely-empty day (history exhausted) from a fetch
    failure (transient — must not stop a backfill). Raises on hard failure."""
    res = client.fetch_day(date)
    if res.empty or not res.intervals:
        db.sync_log_add(datetime.now(LONDON).isoformat(), "sunsynk",
                        "ok" if res.ok else "error", date, 0,
                        "no data" if res.empty else "fetch failed")
        return 0, res
    n = db.upsert_intervals("solar_intervals", res.intervals)
    db.sync_log_add(datetime.now(LONDON).isoformat(), "sunsynk", "ok", date, n)
    if date == today():
        _sunsynk_sanity_checks(client, res)
    return n, res


def _sunsynk_sanity_checks(client, res) -> None:
    """Best-effort cross-checks for today's fetch (never raises):
    - integral vs the realtime `etoday` scalar — catches kW-unit and
      series-matcher drift (CLAUDE.md gotcha #1);
    - last record age — surfaces inverter clock drift / upload lag (#12)."""
    try:
        rt = client.get_realtime()
        scalar = (rt.get("today") or {}).get("pv_kwh")
        if scalar and res.pv_kwh and scalar > 0.5:
            drift = abs(res.pv_kwh - scalar) / scalar
            if drift > 0.25:
                log.warning("Sunsynk /day PV integral %.2f kWh diverges from "
                            "etoday %.2f kWh (%.0f%%) — check _SERIES_MATCHERS "
                            "/ unit handling", res.pv_kwh, scalar, drift * 100)
    except Exception:  # noqa: BLE001, S110 — diagnostics only
        pass
    try:
        if res.intervals:
            last_ts = _parse_local(res.intervals[-1]["interval_start"])
            age = datetime.now(LONDON) - last_ts
            if age > timedelta(minutes=90):
                log.warning("Sunsynk's newest record for today is %dmin old — "
                            "possible inverter clock drift or upload lag",
                            age.total_seconds() // 60)
    except Exception:  # noqa: BLE001, S110 — diagnostics only
        pass


def sync_octopus_day(octo, meters, date: str, gas_units: str = "m3",
                     only: set[str] | None = None) -> dict:
    """Fetch + store Octopus import/export/gas for one day, each priced on the
    tariff agreement in force THAT day. `only` limits it to some sources
    (backfill gap-filling). Returns per-source counts; logs each. Never raises
    for an individual source."""
    counts = {}
    now = datetime.now(LONDON).isoformat()
    tables = {"octopus_elec": "electricity_intervals",
              "octopus_export": "export_intervals",
              "octopus_gas": "gas_intervals"}

    def _run(source, fn):
        try:
            day = fn()
            table = tables[source]
            unpriced = _keep_stored_prices(table, date, day.intervals)
            n = db.upsert_intervals(table, day.intervals)
            status = "partial" if (day.partial or unpriced) else "ok"
            notes = []
            if day.partial:
                notes.append(f"{day.count}/{expected_intervals(date)} intervals")
            if unpriced:
                notes.append(f"{unpriced} unpriced")
            db.sync_log_add(now, source, status, date, n, ", ".join(notes) or None)
            counts[source] = n
        except (octopus.OctopusError, KeyError, ValueError) as e:
            db.sync_log_add(now, source, "error", date, 0, str(e)[:200])
            counts[source] = 0
            log.warning("%s sync failed for %s: %s", source, date, e)

    def _wanted(source, kind):
        if only is not None and source not in only:
            return None
        tariff = meters.tariff_on(kind, date)
        if not tariff and (meters.agreements or {}).get(kind):
            return None                   # not supplied by Octopus that day
        return tariff

    if meters.import_mpan and meters.import_serial:
        tariff = _wanted("octopus_elec", "import")
        if tariff is not None:
            rates = _rates_for_day(octo, tariff, "electricity", date)
            _run("octopus_elec", lambda: octo.fetch_electricity_import(
                meters.import_mpan, meters.import_serial, date, rates,
                tariff or None))
    if meters.export_mpan and meters.export_serial:
        tariff = _wanted("octopus_export", "export")
        if tariff is not None:
            xrates = _rates_for_day(octo, tariff, "electricity", date)
            _run("octopus_export", lambda: octo.fetch_electricity_export(
                meters.export_mpan, meters.export_serial, date, xrates))
    if meters.mprn and meters.gas_serial:
        tariff = _wanted("octopus_gas", "gas")
        if tariff is not None:
            grates = _rates_for_day(octo, tariff, "gas", date)
            _run("octopus_gas", lambda: octo.fetch_gas(
                meters.mprn, meters.gas_serial, date, grates, units=gas_units))
    return counts


def _local_noon(date: str) -> datetime:
    return datetime.fromisoformat(date).replace(hour=12, tzinfo=LONDON)


def _standing_charges(octo, meters, date: str | None = None):
    """(elec, gas) standing charge p/day in force on `date` (default: now),
    from the tariff agreement of that day — a backfilled day gets the charge
    that actually applied then, on the tariff it was on then."""
    elec = gas = None
    at = _local_noon(date) if date else None
    try:
        imp = meters.tariff_on("import", date) if date else meters.import_tariff_code
        g = meters.tariff_on("gas", date) if date else meters.gas_tariff_code
        if imp:
            p, _ = octopus.parse_tariff_code(imp)
            elec = octo.get_standing_charge(p, imp, "electricity", at)
        if g:
            p, _ = octopus.parse_tariff_code(g)
            gas = octo.get_standing_charge(p, g, "gas", at)
    except octopus.OctopusError as e:
        log.warning("Standing charge fetch failed: %s", e)
    return elec, gas


def _stored_flex_rate(date: str) -> float | None:
    with db.conn() as c:
        r = c.execute("SELECT rate_p_per_kwh FROM tariff_rates "
                      "WHERE fuel='electricity_flexible' AND valid_from=?",
                      (date,)).fetchone()
    return r["rate_p_per_kwh"] if r else None


# ---------- orchestration: daily + backfill -------------------------------

def run_daily(date: str | None = None) -> dict:
    """Fetch one day (default: yesterday) from all sources, upsert intervals,
    recompute the daily summary. Returns a result dict. Brief posting is added
    by sync_daily.py in Phase 7. Degrades gracefully if a source is down."""
    date = date or yesterday()
    settings = config.Settings.load()
    result = {"date": date, "sunsynk": 0, "octopus": {}, "errors": []}

    if not (settings.sunsynk_configured or settings.octopus_configured):
        # Writing a summary here stored an all-zero "phantom" day every run —
        # after the 2026-10 key change, three weeks of them looked like real
        # empty days. Nothing configured = nothing written.
        why = ("stored credentials can't be decrypted — re-enter them in Settings"
               if config.unreadable_credentials() else "add one in Settings")
        log.warning("Sync skipped for %s: no data source configured (%s)", date, why)
        result["errors"].append("no data source configured")
        result["skipped"] = True
        return result

    if settings.sunsynk_configured:
        sc = sunsynk.SunsynkClient(settings)
        try:
            result["sunsynk"], _ = sync_sunsynk_day(sc, date)
        except sunsynk.SunsynkError as e:
            result["errors"].append(f"sunsynk: {e}")
            db.sync_log_add(datetime.now(LONDON).isoformat(), "sunsynk",
                            "error", date, 0, str(e)[:200])
        finally:
            sc.close()

    std_elec = std_gas = None
    if settings.octopus_configured:
        octo = None
        try:
            octo = octopus.OctopusClient(settings.octopus_api_key,
                                         settings.octopus_account_number)
            meters = resolve_meters(settings, octo)
            # Persist the active import tariff code so brief/planner can gate
            # Cosy-specific wording without an extra discovery call. Never
            # CLEARED here: a blank code means discovery failed, not that the
            # account stopped having a tariff.
            if meters.import_tariff_code:
                try:
                    config.set_text("import_tariff_code", meters.import_tariff_code)
                except Exception:  # noqa: BLE001, S110 — cosmetic metadata
                    pass
            result["octopus"] = sync_octopus_day(
                octo, meters, date,
                gas_units=getattr(settings, "gas_units", "m3") or "m3")
            std_elec, std_gas = _standing_charges(octo, meters, date)
            try:
                # The Flexible counterfactual changes quarterly at most; it is
                # cached per synced date in tariff_rates, so the hourly
                # intraday runs don't re-walk the /products/ listing.
                flex = _stored_flex_rate(date)
                if flex is None and meters.region:
                    flex = octo.get_flexible_import_rate(meters.region,
                                                         at=_local_noon(date))
                    if flex:   # cache for the savings trend
                        db.upsert_tariff_rate("electricity_flexible", date, flex)
                result["flex_rate_p"] = flex
            except octopus.OctopusError as e:
                log.warning("Flexible rate fetch failed: %s", e)
        except octopus.OctopusError as e:
            result["errors"].append(f"octopus: {e}")
        finally:
            if octo is not None:
                octo.close()

    if not db.has_any_intervals(date):
        # Both sources failed or had nothing: an all-zero row here rendered as
        # "£0.00 — confirmed" and skewed every average (2026-10-07 rule).
        # The brief still posts — degraded — from a live computation.
        log.warning("No data from any source for %s — no summary written", date)
        result["no_data"] = True
        return result
    summary = compute_daily_summary(date, standing_elec_p=std_elec,
                                    standing_gas_p=std_gas)
    db.upsert_daily_summary(summary)
    result["summary"] = summary
    return result


def run_recent_catchup(days_back: int = 2) -> None:
    """Sync today, plus re-sync any of the last `days_back` days that aren't
    fully settled. Octopus import settles over the following day, and a restart
    can miss the 06:25 master run — this self-heals both. Fully settled days
    are skipped, so we don't re-hit the APIs needlessly.

    "Settled" is judged per configured source from the stored intervals (all
    of the day's half-hours, all priced) — not from summary flags, which
    re-synced an `export_estimated` day every hour forever when no export
    meter exists, and never noticed late gas."""
    run_daily(today())
    t = _today_local()
    settings = config.Settings.load()
    meters = resolve_meters(settings) if settings.octopus_configured else None
    for back in range(1, days_back + 1):
        ds = (t - timedelta(days=back)).isoformat()
        s = db.get_daily_summary(ds)
        needs = (s is None or bool(s["data_partial"])
                 or (meters is not None and bool(_octopus_gaps(ds, meters)))
                 or (settings.sunsynk_configured and not s["sunsynk_ok"]))
        if needs:
            log.info("Catch-up re-sync of unsettled day %s", ds)
            run_daily(ds)


_LAST_BRIEF_KEY = "last_brief_date"
_BRIEF_STATUS_KEY = "brief_status"
# The brief counts as late (health/heartbeat fail) from this local hour: the
# master runs at 06:25 and retries its webhook for up to ~a minute.
_BRIEF_DUE_HOUR = 7
# Serialises read-dedup-key → post → set-key so the 06:25 master and an
# intraday tick can never both post the same day's brief.
_brief_lock = threading.Lock()


def _brief_attempt() -> dict:
    try:
        return json.loads(db.setting_get(_BRIEF_STATUS_KEY) or "{}")
    except (ValueError, TypeError):
        return {}


def _record_brief(date: str, ok: bool, detail: str, *, via: str) -> None:
    """Record a delivery attempt (shown in Settings and /api/health). The
    dedup key only ever moves FORWARD: a deliberate re-send of an old date
    used to set it backwards, and the next tick re-posted yesterday's brief."""
    prev = _brief_attempt()
    attempts = (prev.get("attempts", 1) + 1
                if prev.get("date") == date and not prev.get("ok") else 1)
    db.setting_set(_BRIEF_STATUS_KEY, json.dumps({
        "date": date, "ok": bool(ok), "detail": (detail or "")[:200],
        "via": via, "attempts": attempts,
        "at": datetime.now(LONDON).isoformat(timespec="seconds")}))
    if ok:
        last = db.setting_get(_LAST_BRIEF_KEY)
        if not last or date > last:
            db.setting_set(_LAST_BRIEF_KEY, date)


def brief_status() -> dict:
    """Delivery receipt for the daily brief (O2).

    ok: None = not this instance's job (no webhook, or a standby);
        True = yesterday's brief went out, or it isn't due yet (07:00);
        False = it is due and has not been delivered. A failed brief used to
        be visible only as an INFO log line, and was silently dropped at
        midnight."""
    now = datetime.now(LONDON)
    due_date = (now.date() - timedelta(days=1)).isoformat()
    last = db.setting_get(_LAST_BRIEF_KEY)
    attempt = _brief_attempt() or None
    ok: bool | None
    if (not config.Settings.load().mattermost_configured
            or config.instance_role() == "standby"):
        ok = None
    else:
        ok = bool(last and last >= due_date) or now.hour < _BRIEF_DUE_HOUR
    return {"ok": ok, "last_delivered": last, "due_date": due_date,
            "last_attempt": attempt, "role": config.instance_role()}


def _undelivered_before(date: str) -> dict | None:
    """A brief for an earlier day that failed and was never delivered."""
    a = _brief_attempt()
    last = db.setting_get(_LAST_BRIEF_KEY)
    if (a.get("date") and a["date"] < date and not a.get("ok")
            and (not last or last < a["date"])):
        return {"date": a["date"], "detail": a.get("detail", "")}
    return None


def _peer_health() -> tuple[dict | None, str]:
    """The configured peer instance's /api/health data, or (None, why)."""
    url = config.peer_url()
    if not url:
        return None, "no peer configured"
    try:
        r = requests.get(url + "/api/health", timeout=5,
                         headers={"User-Agent": "PowerSOC"})
        data = (r.json() or {}).get("data")
        if r.status_code != 200 or not isinstance(data, dict):
            return None, f"peer health returned HTTP {r.status_code}"
        return data, ""
    except (requests.RequestException, ValueError, AttributeError) as e:
        return None, f"peer unreachable ({type(e).__name__})"


def _brief_turn(date: str) -> tuple[bool, str]:
    """Whether THIS instance should post `date`'s brief, and why (O3).
    A primary always does. A standby only covers for its peer: when the
    peer is unreachable, isn't a primary, or is due and hasn't delivered."""
    if config.instance_role() != "standby":
        return True, ""
    if not config.peer_url():
        return False, "standby with no peer configured"
    peer, why = _peer_health()
    if peer is None:
        return True, f"the primary is unreachable ({why})"
    if (peer.get("instance") or {}).get("role") != "primary":
        return True, "the peer is not a primary"
    if ((peer.get("brief") or {}).get("last_delivered") or "") >= date:
        return False, "the primary delivered it"
    if peer.get("brief_ok") is False:
        return True, "the primary did not deliver it"
    return False, "the primary's brief is not due yet"


def _deliver_brief(date: str, *, via: str, flex_rate_p=None,
                   standby_why: str = "") -> tuple[bool, str]:
    """Build, post and record one brief. Caller holds _brief_lock."""
    import brief
    import notifications
    undelivered = _undelivered_before(date)
    if undelivered:
        log.error("The brief for %s was never delivered (%s)",
                  undelivered["date"], undelivered["detail"])
        msg = brief.build_daily_brief(date, flex_rate_p=flex_rate_p,
                                      undelivered=undelivered)
    else:
        msg = brief.build_daily_brief(date, flex_rate_p=flex_rate_p)
    if config.instance_role() == "standby":
        msg += (f"\n\n_⚠️ Sent by standby **{config.instance_name()}** — "
                f"{standby_why or 'covering for the primary'}._")
    sent, detail = notifications.post_brief(msg)
    _record_brief(date, sent, detail, via=via)
    if sent:
        log.info("Brief for %s delivered (%s): %s", date, via, detail)
    else:
        log.warning("Brief NOT delivered for %s (%s): %s", date, via, detail)
    return sent, detail


def run_daily_and_notify(date: str | None = None) -> dict:
    """run_daily() plus posting the Mattermost brief. Used by the 06:25 master
    and the cron CLI. The brief uses estimated import from the Sunsynk CT when
    Octopus is partial (within ~2% on our meter), so it's meaningful even at
    the early hour when SMETS2 hasn't settled.

    The scheduler path (no explicit date) dedups against last_brief_date so a
    day whose brief already went out (e.g. via the intraday safety net) is not
    posted twice, and leaves it to the primary on a standby; an explicit-date
    CLI run always posts (deliberate re-send)."""
    explicit = date is not None
    result = run_daily(date)
    try:
        with _brief_lock:
            d = result["date"]
            last = db.setting_get(_LAST_BRIEF_KEY)
            if not explicit and last and last >= d:
                log.info("Brief for %s already posted — skipping duplicate", d)
                result["brief_sent"] = False
            else:
                turn, why = (True, "") if explicit else _brief_turn(d)
                if not turn:
                    log.info("Brief for %s not posted by this instance (%s)", d, why)
                    result["brief_sent"] = False
                else:
                    result["brief_sent"], _ = _deliver_brief(
                        d, via="cli" if explicit else "master",
                        flex_rate_p=result.get("flex_rate_p"), standby_why=why)
    except Exception:  # noqa: BLE001 — brief delivery must never crash the run
        log.exception("Brief delivery failed")
    return result


def post_brief_if_ready(date: str) -> bool:
    """Safety net, called from the intraday loop: posts `date`'s brief if it
    hasn't been delivered — a missed master run (restart through 06:25) or a
    failed delivery (webhook down) — retrying hourly. On a normal day this
    is a cheap no-op.

    Guards (both bugs observed in review): never fires before the master slot
    (the first tick after midnight would otherwise post a premature brief from
    unsettled data, then the 06:25 master would post again), and on a fresh
    install with no summary yet it just sets the baseline instead of posting
    mid-backfill."""
    if datetime.now(LONDON).hour < _BRIEF_DUE_HOUR:
        return False
    if not config.Settings.load().mattermost_configured:
        # Nowhere to post: retrying (and logging a failure) every hour adds
        # nothing — the brief catches up on the first tick after it's set.
        return False
    with _brief_lock:
        last = db.setting_get(_LAST_BRIEF_KEY)
        if last and last >= date:
            return False
        if last is None and db.get_daily_summary(date) is None:
            db.setting_set(_LAST_BRIEF_KEY, date)
            return False
        turn, why = _brief_turn(date)
        if not turn:
            return False
        prev = _brief_attempt()
        if prev.get("date") == date and not prev.get("ok"):
            log.info("Brief for %s not delivered yet — retrying (attempt %d; "
                     "last: %s)", date, prev.get("attempts", 1) + 1,
                     prev.get("detail"))
        else:
            log.info("No brief recorded for %s — posting now (the 06:25 run "
                     "didn't post it)", date)
        sent, _ = _deliver_brief(
            date, via="catchup", standby_why=why,
            flex_rate_p=db.latest_tariff_rate("electricity_flexible"))
        return sent


def resend_brief(date: str) -> tuple[bool, str]:
    """Deliberate re-send (Settings → Resend): always posts, records it."""
    with _brief_lock:
        return _deliver_brief(date, via="resend",
                              flex_rate_p=_stored_flex_rate(date)
                              or db.latest_tariff_rate("electricity_flexible"))


# ---------- dead-man's switch ---------------------------------------------

def heartbeat(ok: bool = True) -> None:
    """Ping POWERSOC_HEARTBEAT_URL (healthchecks.io / Uptime Kuma push).

    The external monitor alerts when pings STOP — the only way to notice a
    box that is down entirely (an outage on 2026-09 went unnoticed because
    nothing off-box was watching). Never raises; the URL is treated as a
    secret (never logged)."""
    url = config.heartbeat_url()
    if not url:
        return
    if not ok:
        url = url.rstrip("/") + "/fail"
    try:
        requests.get(url, timeout=10, headers={"User-Agent": "PowerSOC"})
    except Exception as e:  # noqa: BLE001 — monitoring must never break sync
        log.warning("Heartbeat ping failed: %s", type(e).__name__)


def ingest_ok() -> bool:
    """Whether data CAN be flowing: a source is configured and no stored
    credential is unreadable. The heartbeat used to ping "ok" every hour
    regardless, so a monitor would have stayed green through the 2026-10
    three-week gap."""
    try:
        s = config.Settings.load()
        return ((s.sunsynk_configured or s.octopus_configured)
                and not config.unreadable_credentials())
    except Exception:  # noqa: BLE001 — a broken config is not "ok"
        return False


def backup_status() -> dict:
    """ok: None = no backup attempted yet (fresh install); False = the last
    attempt failed or the newest backup is over 26 h old; True otherwise."""
    from datetime import datetime as _dt
    err = db.setting_get("last_backup_error")
    raw = db.setting_get("last_backup_at")
    age = None
    if raw:
        try:
            age = (_dt.now(timezone.utc) - _dt.fromisoformat(raw)).total_seconds()
        except (ValueError, TypeError):
            age = None
    if err:
        ok: bool | None = False
    elif age is None:
        ok = None
    else:
        ok = age < 26 * 3600
    return {"ok": ok, "error": err, "last_at": raw, "age_s": age,
            "isolated": config.backup_dir_isolated()}


def scheduler_ok() -> bool:
    """What the heartbeat reports: data can flow, the brief isn't overdue and
    the nightly backup isn't failing. A failed brief (a login wall in front of
    the webhook) or backup used to leave the heartbeat green."""
    try:
        return (ingest_ok() and brief_status()["ok"] is not False
                and backup_status()["ok"] is not False)
    except Exception:  # noqa: BLE001 — a broken status check is not "ok"
        return False


def daily_housekeeping() -> None:
    """Backup + retention, run after the master sync (scheduler and cron)."""
    try:
        n = db.prune_phantom_summaries()
        if n:
            log.info("Removed %d summaries for days with no stored data", n)
    except Exception:  # noqa: BLE001
        log.exception("Phantom-summary prune failed")
    try:
        path = db.backup_database()
        log.info("DB backup written: %s", path)
        db.setting_set("last_backup_error", None)
        if not config.backup_dir_isolated():
            log.warning("Backups are on the same disk as the data (%s) — one "
                        "disk failure takes both. Mirror them elsewhere "
                        "(POWERSOC_BACKUP_MIRROR_DIR) or copy them off-box.",
                        config.BACKUP_DIR)
    except Exception as e:  # noqa: BLE001 — backup must never kill the loop
        # Recorded, not just logged: health, the heartbeat and the brief all
        # report it (a failing backup used to be invisible).
        log.exception("DB backup failed")
        try:
            db.setting_set("last_backup_error",
                           f"{type(e).__name__}: {e}"[:200])
        except Exception:  # noqa: BLE001, S110 — the DB itself may be the problem
            pass
    import power_quality
    for fn, what in ((db.prune_sync_log, "sync_log"),
                     (db.prune_audit_failures, "failed-login audit"),
                     (power_quality.prune, "grid sample")):
        try:
            n = fn()
            if n:
                log.info("Pruned %d old %s rows", n, what)
        except Exception:  # noqa: BLE001
            log.exception("%s prune failed", what)


# ---------- in-app scheduler (Docker; no cron) ----------------------------

def _seconds_until(now: datetime, hour: int, minute: int) -> float:
    """REAL seconds from `now` to the next local hour:minute (same tz).

    Goes through .timestamp(): subtracting two datetimes that share a tzinfo is
    WALL-CLOCK arithmetic in Python, which made the master run fire at 05:25
    and again at 06:25 on the autumn DST day (and at 07:25 in spring)."""
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target = (target + timedelta(days=1)).replace(hour=hour, minute=minute)
    # Re-attach the zone rule for the target's own date (fold/offset may differ).
    target = datetime(target.year, target.month, target.day, hour, minute,
                      tzinfo=now.tzinfo)
    return max(0.0, target.timestamp() - now.timestamp())


def run_daily_scheduler(hour: int = 6, minute: int = 25) -> None:
    """Loop forever: sleep until hour:minute Europe/London, run the MASTER daily
    sync (yesterday, authoritative) + brief, and refresh tariff rates on
    Sundays. The brief's import line uses the Sunsynk CT estimate when Octopus
    is still partial — within ~2% of the eventual authoritative value."""
    log.info("Daily scheduler running (master %02d:%02d Europe/London)", hour, minute)
    while True:
        time.sleep(_seconds_until(datetime.now(LONDON), hour, minute))
        ok = True
        try:
            run_daily_and_notify()                    # yesterday + brief
        except Exception:  # noqa: BLE001 — keep the scheduler alive
            ok = False
            log.exception("Master daily run failed")
        try:
            # Daily (not weekly): stores today's + tomorrow's rate periods so
            # the live "current rate" resolves on time-of-use tariffs.
            refresh_tariff_rates()
        except Exception:  # noqa: BLE001
            log.exception("Tariff rate refresh failed")
        daily_housekeeping()
        heartbeat(ok and scheduler_ok())
        time.sleep(60)   # step past the trigger minute to avoid a double-run


def intraday_tick() -> None:
    """One intraday round: refresh today (+ unsettled recent days), the brief
    safety net, alerts, a missing current rate, and the scheduler heartbeat.
    The jobs are independently guarded: a sync failure (e.g. a Sunsynk
    outage) must not suppress the brief safety net — and especially not the
    alerts, whose whole purpose includes reporting that very outage. Run by
    the in-app scheduler, and hourly from cron on host installs (which had no
    catch-up, safety net or alerts at all)."""
    try:
        run_recent_catchup()   # today + re-sync unsettled recent days
    except Exception:  # noqa: BLE001 — keep the scheduler alive
        log.exception("Intraday refresh failed")
    try:
        # Post yesterday's brief if the morning master was missed or failed.
        post_brief_if_ready(yesterday())
    except Exception:  # noqa: BLE001
        log.exception("Catch-up brief failed")
    try:
        import alerts
        alerts.check_alerts(today())
    except Exception:  # noqa: BLE001
        log.exception("Alert check failed")
    try:
        # Self-heal a missing current rate (fresh install, restart before
        # the first master run) so /api/live can show it. Cheap no-op once
        # today's periods are stored.
        if (config.Settings.load().octopus_configured
                and db.rate_at("electricity_import",
                               datetime.now(LONDON).isoformat()) is None):
            refresh_tariff_rates()
    except Exception:  # noqa: BLE001
        log.exception("Rate coverage check failed")
    try:
        _daily_extras()
    except Exception:  # noqa: BLE001
        log.exception("Daily extras failed")
    try:
        # Surfaced via /api/health (and the watchdog) so a dead scheduler is
        # observable from the dashboard / uptime checks.
        db.setting_set("scheduler_last_tick", datetime.now(LONDON).isoformat())
    except Exception:  # noqa: BLE001, S110 — heartbeat is best-effort
        pass


def _daily_extras() -> None:
    """Once a day from the intraday tick: refresh the actual-radiation history
    that calibrates the solar forecast, and keep one BMS snapshot for the
    battery state-of-health trend."""
    d = today()
    if db.setting_get("daily_extras_date") == d:
        return
    db.setting_set("daily_extras_date", d)
    import solar_forecast
    solar_forecast.refresh_history()
    if config.Settings.load().sunsynk_configured:
        import insights
        from routes.payloads import _indepth_snapshot
        try:
            insights.record_battery_snapshot(_indepth_snapshot())
        except sunsynk.SunsynkError as e:
            log.warning("Battery snapshot skipped: %s", e)


def run_intraday_scheduler(interval_s: int = 3600) -> None:
    """Loop forever: an intraday_tick() every interval_s. Octopus import + gas
    settle through the day; export shows the Sunsynk estimate until the
    master run reconciles it."""
    log.info("Intraday scheduler running (every %d min)", interval_s // 60)
    time.sleep(120)   # let startup settle before the first run
    next_t = time.monotonic()
    while True:
        intraday_tick()
        heartbeat(scheduler_ok())
        # Fixed-schedule sleep: ticks don't drift by the run's duration.
        next_t += interval_s
        time.sleep(max(1.0, next_t - time.monotonic()))


_scheduler_started = False


def watchdog_reason(threads_alive: dict[str, bool], last_tick: str | None,
                    now: datetime, interval_s: int,
                    started_at: datetime) -> str | None:
    """Why the scheduler is broken beyond self-repair, or None. Pure.

    The container healthcheck only ever REPORTED trouble (nothing acts on
    "unhealthy"); a dead or wedged scheduler thread meant no sync, brief or
    backup until someone noticed."""
    dead = [n for n, alive in threads_alive.items() if not alive]
    if dead:
        return f"scheduler thread(s) died: {', '.join(sorted(dead))}"
    limit = timedelta(seconds=3 * interval_s + 600)
    if now - started_at < limit:
        return None                           # still within the first ticks
    try:
        tick = datetime.fromisoformat(last_tick) if last_tick else None
    except ValueError:
        tick = None
    if tick is None or now - tick > limit:
        return f"no intraday tick since {last_tick or 'start-up'}"
    return None


def _run_watchdog(threads: dict, interval_s: int) -> None:
    """Exit the process when the scheduler is dead/wedged, so the supervisor
    (gunicorn re-forks the worker; Docker/systemd restart) brings it back."""
    import os
    started = datetime.now(LONDON)
    while True:
        time.sleep(300)
        try:
            reason = watchdog_reason({n: t.is_alive() for n, t in threads.items()},
                                     db.setting_get("scheduler_last_tick"),
                                     datetime.now(LONDON), interval_s, started)
        except Exception:  # noqa: BLE001 — a DB hiccup is not a dead scheduler
            log.exception("Watchdog check failed")
            continue
        if reason:
            log.critical("Watchdog: %s — exiting so the process is restarted", reason)
            os._exit(3)


def start_scheduler() -> bool:
    """Start the daily + intraday schedulers (and their watchdog) in daemon
    threads (once)."""
    global _scheduler_started
    if _scheduler_started:
        return False
    _scheduler_started = True
    interval_s = config.INTRADAY_MINUTES * 60
    threads = {
        "daily": threading.Thread(target=run_daily_scheduler, name="powersoc-daily",
                                  daemon=True),
        "intraday": threading.Thread(target=run_intraday_scheduler,
                                     kwargs={"interval_s": interval_s},
                                     name="powersoc-intraday", daemon=True),
    }
    for t in threads.values():
        t.start()
    if config.WATCHDOG_ENABLED:
        threading.Thread(target=_run_watchdog, args=(threads, interval_s),
                         name="powersoc-watchdog", daemon=True).start()
    return True


# Backfill progress is shared with the web layer via this module-level state.
_backfill_lock = threading.Lock()
_backfill_state = {"running": False, "mode": "backfill", "days_done": 0,
                   "days_total": 0, "skipped": 0, "error": None}


def backfill_status() -> dict:
    with _backfill_lock:
        st = dict(_backfill_state)
    if not st["running"] and not st["days_total"]:
        # Nothing this process run — fall back to the persisted state so the
        # Settings progress panel survives a container restart.
        raw = db.setting_get("backfill_state")
        if raw:
            try:
                saved = json.loads(raw)
                saved["running"] = False
                return saved
            except (ValueError, TypeError):
                pass
    return st


def _set_backfill(**kw):
    with _backfill_lock:
        _backfill_state.update(kw)
        snap = dict(_backfill_state)
    try:
        db.setting_set("backfill_state", json.dumps(snap))
    except Exception:  # noqa: BLE001, S110 — progress persistence is best-effort
        pass


def _already_stored(d: date_cls, today: date_cls, force: bool) -> bool:
    """Whether a backfill day's SUMMARY can be left alone: not forced, older
    than the 2-day settling window, and already useful. A summary written
    after a total fetch failure (all-zero, no source ok), one still carrying
    a CT estimate, or one whose import is unpriced must be recomputed."""
    if force or (today - d).days <= 2:
        return False
    s = db.get_daily_summary(d.isoformat())
    if s is None:
        return False
    if s["octopus_ok"] and s["import_cost_pence"] is None:
        return False
    return bool((s["octopus_ok"] or s["sunsynk_ok"])
                and not s["data_partial"] and not s["import_estimated"])


# Consecutive genuinely-empty Sunsynk days before we conclude the inverter's
# history is exhausted. A single empty (or failed) day must not stop the walk:
# one 502 or a power-cut day would silently skip ALL deeper solar history.
_SUNSYNK_EMPTY_STREAK = 3


def run_backfill(start: str | None = None, max_days: int = 730,
                 force: bool = False) -> dict:
    """Deep history backfill, per source: each day fetches only what is
    missing — an Octopus source short of half-hours or holding an unpriced
    one (and only on days an agreement covers), Sunsynk when the day has no
    solar rows — so a re-run costs API calls only for real gaps. force=True
    re-fetches every source for every day (e.g. after a meter change).

    No summary is written for a day nothing returned data for: older
    versions wrote one for every day of the range, so the months before an
    account's history began became standing-charge-only "phantom" days."""
    octo = meters = sc = None
    try:
        # Everything — including argument parsing and client/meter init — runs
        # inside try/finally: a bad start date or a network error here used to
        # leave _backfill_state.running stuck True until a restart.
        settings = config.Settings.load()
        today = _today_local()
        start_date = (date_cls.fromisoformat(start) if start
                      else today - timedelta(days=max_days))
        days = list(_daterange(start_date, today - timedelta(days=1)))
        _set_backfill(running=True, mode="backfill", days_done=0,
                      days_total=len(days), skipped=0, error=None)
        log.info("Backfill starting: %d days from %s", len(days), start_date)

        if settings.octopus_configured:
            try:
                octo = octopus.OctopusClient(settings.octopus_api_key,
                                             settings.octopus_account_number)
                meters = resolve_meters(settings, octo)
            except octopus.OctopusError as e:
                log.warning("Backfill: Octopus init failed: %s", e)
                octo = meters = None

        sc = sunsynk.SunsynkClient(settings) if settings.sunsynk_configured else None
        if octo is None and sc is None:
            # No source to fetch from: walking the range would only write
            # hundreds of all-zero summaries.
            log.warning("Backfill not run: no data source available")
            _set_backfill(running=False, error="No data source configured "
                          "(or Octopus unreachable) — check Settings")
            return backfill_status()
        empty_streak = 0
        sunsynk_exhausted = False
        gas_units = getattr(settings, "gas_units", "m3") or "m3"

        # Walk newest-first so Sunsynk's "stop when empty" works and the most
        # recent (most useful) days populate first.
        skipped = 0
        for i, d in enumerate(reversed(days)):
            ds = d.isoformat()
            need = _octopus_gaps(ds, meters, force) if (octo and meters) else set()
            has_solar = bool(db.intervals_for_date("solar_intervals", ds))
            if has_solar:
                empty_streak = 0          # stored history: not end-of-history
            sun_need = bool(sc) and not sunsynk_exhausted and (force or not has_solar)
            if not need and not sun_need:
                # Nothing to fetch. Recompute a stale summary locally (no API
                # calls beyond the per-tariff standing-charge cache).
                if db.has_any_intervals(ds) and not _already_stored(d, today, False):
                    _write_summary(octo, meters, ds)
                skipped += 1
                _set_backfill(days_done=i + 1, skipped=skipped)
                continue
            if need:
                sync_octopus_day(octo, meters, ds, gas_units=gas_units, only=need)
            if sun_need:
                try:
                    _, res = sync_sunsynk_day(sc, ds)
                    if res.empty:
                        empty_streak += 1
                        if empty_streak >= _SUNSYNK_EMPTY_STREAK:
                            sunsynk_exhausted = True
                            log.info("Sunsynk history exhausted around %s "
                                     "(%d consecutive empty days)",
                                     ds, empty_streak)
                    elif res.ok:
                        empty_streak = 0
                    # ok=False (transient fetch failure): leave the streak
                    # untouched — never treat an outage as end-of-history.
                except sunsynk.SunsynkAuthError as e:
                    # A definitive rejection won't change tomorrow: retrying
                    # every day was ~2 logins (modern + legacy) per day of
                    # history — ~1,460 failed logins, risking an account lock.
                    log.warning("Backfill: Sunsynk rejected the login (%s) — "
                                "skipping Sunsynk for the rest of this run", e)
                    sunsynk_exhausted = True
                except sunsynk.SunsynkError as e:
                    log.warning("Backfill Sunsynk %s: %s", ds, e)
            if db.has_any_intervals(ds):
                _write_summary(octo, meters, ds)
            _set_backfill(days_done=i + 1)
            time.sleep(0.3)   # pacing: stay polite to the upstream APIs
        log.info("Backfill finished: %d days (%d already stored, skipped)",
                 len(days), skipped)
    except Exception as e:  # noqa: BLE001 — never let the thread die silently
        log.exception("Backfill aborted")
        _set_backfill(error=str(e))
    finally:
        if octo:
            octo.close()
        if sc:
            sc.close()
        _set_backfill(running=False)
    return backfill_status()


def fix_battery_orientation() -> int:
    """Repair stored days whose battery charge/discharge columns are swapped
    (written before the per-day orientation check in sunsynk.battery_sign):
    a day is swapped when its 'charge' coincides with SoC FALLING. Per day,
    from the stored half-hours; idempotent. Returns the days repaired."""
    with db.conn() as c:
        days = [r[0] for r in c.execute(
            "SELECT DISTINCT substr(interval_start, 1, 10) FROM solar_intervals")]
    fixed = 0
    for d in days:
        rows = [r for r in db.intervals_for_date("solar_intervals", d)
                if r["soc_pct"] is not None]
        votes = 0
        for a, b in zip(rows, rows[1:]):
            ds = b["soc_pct"] - a["soc_pct"]
            net = (b["battery_charge_kwh"] or 0) - (b["battery_discharge_kwh"] or 0)
            if abs(ds) >= 1 and abs(net) > 0.05:
                votes += 1 if (ds > 0) == (net > 0) else -1
        if votes >= 0:
            continue
        nxt = (date_cls.fromisoformat(d) + timedelta(days=1)).isoformat()
        with db.conn() as c:
            c.execute("BEGIN")
            c.execute("UPDATE solar_intervals SET battery_charge_kwh=battery_discharge_kwh, "
                      "battery_discharge_kwh=battery_charge_kwh "
                      "WHERE interval_start >= ? AND interval_start < ?", (d, nxt))
            c.execute("UPDATE daily_summaries SET battery_charge_kwh=battery_discharge_kwh, "
                      "battery_discharge_kwh=battery_charge_kwh WHERE date=?", (d,))
            c.execute("COMMIT")
        fixed += 1
    if fixed:
        log.warning("Repaired %d stored day(s) with battery charge/discharge swapped", fixed)
    return fixed


def _write_summary(octo, meters, ds: str) -> None:
    std_elec, std_gas = (_standing_charges(octo, meters, ds)
                         if (octo and meters) else (None, None))
    db.upsert_daily_summary(compute_daily_summary(
        ds, standing_elec_p=std_elec, standing_gas_p=std_gas))


# ---------- re-pricing stored history ---------------------------------------

class _RateIndex:
    """Rate periods sorted for O(log n) lookup by instant (a year of Cosy is
    ~2.5k periods against ~17k half-hours; octopus.rate_for is a linear scan)."""

    def __init__(self, rates: list[dict]):
        rows = []
        for r in rates or []:
            if r.get("value_inc_vat") is None:
                continue
            rows.append((octopus._aware_epoch(r.get("valid_from"), float("-inf")),
                         octopus._aware_epoch(r.get("valid_to"), float("inf")),
                         r["value_inc_vat"]))
        rows.sort()
        self.rows = rows
        self.starts = [r[0] for r in rows]

    def at(self, epoch: float) -> float | None:
        import bisect
        i = bisect.bisect_right(self.starts, epoch) - 1
        # The latest-starting period that still covers the instant wins (as in
        # octopus.rate_for); walk back a bounded distance past any gap.
        for j in range(i, max(-1, i - 64), -1):
            _start, end, value = self.rows[j]
            if end > epoch:
                return value
        return None

    def day_values(self, day_start: float, day_end: float) -> list[float]:
        import bisect
        lo = max(0, bisect.bisect_right(self.starts, day_start) - 1)
        hi = bisect.bisect_left(self.starts, day_end)
        return [v for s, e, v in self.rows[lo:hi] if e > day_start and s < day_end]


_REPRICE = (("import", "electricity_intervals", "electricity", "consumption_kwh"),
            ("export", "export_intervals", "electricity", "export_kwh"),
            ("gas", "gas_intervals", "gas", "consumption_kwh"))


def _interval_epoch(ts: str) -> float:
    d = datetime.fromisoformat(ts)
    return (d if d.tzinfo else d.replace(tzinfo=LONDON)).timestamp()


def reprice_history(start: str | None = None, end: str | None = None) -> dict:
    """Re-price every stored Octopus interval from the tariff agreement in
    force at that half-hour, then recompute the affected daily summaries
    (with that day's standing charges).

    Repairs history priced on the wrong tariff (a backfill used today's),
    unpriced intervals (the next-day 00:00 row; export with no rates) — from
    one rates request per tariff agreement instead of re-fetching a single
    day of consumption. Never overwrites a stored price with "unknown"."""
    settings = config.Settings.load()
    out = {"intervals": 0, "days": 0, "unpriced": 0, "error": None}
    if not settings.octopus_configured:
        out["error"] = "Octopus not configured"
        return out
    octo = None
    try:
        octo = octopus.OctopusClient(settings.octopus_api_key,
                                     settings.octopus_account_number)
        meters = resolve_meters(settings, octo, force_discover=True)
        lo, hi = start or "0000-01-01", (
            (date_cls.fromisoformat(end) + timedelta(days=1)).isoformat()
            if end else "9999-12-31")
        dates: set[str] = set()
        for kind, table, fuel, qty in _REPRICE:
            windows = meters.tariff_windows(kind)
            rows = db.intervals_between(table, lo, hi)
            if not rows or not windows:
                continue
            groups: dict[str, list] = {}
            for r in rows:
                ep = _interval_epoch(r["interval_start"])
                code = meters.tariff_at(kind, ep)
                if code:
                    groups.setdefault(code, []).append((ep, r))
            for code, items in groups.items():
                first, last = items[0][0], items[-1][0]
                product, _ = octopus.parse_tariff_code(code)
                pf = datetime.fromtimestamp(first, timezone.utc).isoformat()
                pt = datetime.fromtimestamp(last + 1800, timezone.utc).isoformat()
                idx = _RateIndex(octo.get_unit_rates(product, code, fuel, pf, pt))
                banded = kind == "import" and not octopus.is_cosy_product(code)
                day_cache: dict[str, list[float]] = {}
                updates = []
                for ep, r in items:
                    p = idx.at(ep)
                    if p is None:
                        out["unpriced"] += 1
                        if r["rate_p_per_kwh"] is not None:
                            continue     # keep the stored price
                        updates.append((None, None, None, r["interval_start"]))
                        continue
                    window = None
                    if kind == "import":
                        local = datetime.fromtimestamp(ep, LONDON)
                        dkey = local.date().isoformat()
                        if banded and dkey not in day_cache:
                            ds0 = datetime.combine(local.date(), datetime.min.time(), LONDON)
                            day_cache[dkey] = idx.day_values(
                                ds0.timestamp(), (ds0 + timedelta(days=1)).timestamp())
                        window = octopus.classify_window(
                            local, p, day_cache.get(dkey), code)
                    updates.append((p, round((r[qty] or 0) * p, 4), window,
                                    r["interval_start"]))
                    dates.add(r["interval_start"][:10])
                out["intervals"] += db.update_interval_prices(table, updates)
                log.info("Re-priced %d %s intervals on %s", len(updates), kind, code)
        for ds in sorted(dates):
            _write_summary(octo, meters, ds)
        out["days"] = len(dates)
        n = db.prune_phantom_summaries()
        if n:
            log.info("Removed %d summaries for days with no stored data", n)
        log.info("Re-price finished: %d intervals, %d days, %d still unpriced",
                 out["intervals"], out["days"], out["unpriced"])
    except octopus.OctopusError as e:
        log.warning("Re-price failed: %s", e)
        out["error"] = str(e)
    finally:
        if octo is not None:
            octo.close()
    return out


def _run_reprice_job() -> None:
    try:
        res = reprice_history()
        _set_backfill(error=res.get("error"), repriced=res)
    except Exception as e:  # noqa: BLE001 — never let the thread die silently
        log.exception("Re-price aborted")
        _set_backfill(error=str(e))
    finally:
        _set_backfill(running=False)


def start_reprice_async() -> bool:
    """Re-price history in a daemon thread; shares the backfill slot (both
    rewrite the same rows). Returns False if either is already running."""
    with _backfill_lock:
        if _backfill_state["running"]:
            return False
        _backfill_state["running"] = True
    _set_backfill(mode="reprice", days_done=0, days_total=0, skipped=0, error=None)
    threading.Thread(target=_run_reprice_job, name="powersoc-reprice",
                     daemon=True).start()
    return True


def start_backfill_async(start: str | None = None, force: bool = False) -> bool:
    """Start backfill in a daemon thread if not already running. Returns True
    if a new run was started. force=True re-fetches even already-stored days."""
    with _backfill_lock:
        if _backfill_state["running"]:
            return False
        _backfill_state["running"] = True
    t = threading.Thread(target=run_backfill,
                         kwargs={"start": start, "force": force},
                         name="powersoc-backfill", daemon=True)
    t.start()
    return True


def maybe_start_first_run_backfill() -> bool:
    """Kick off a backfill on first run when the DB has no summaries yet and
    credentials are configured. Called by app.py at startup."""
    lo, hi = db.summary_date_bounds()
    if lo is not None:
        return False
    settings = config.Settings.load()
    if not (settings.octopus_configured or settings.sunsynk_configured):
        return False
    return start_backfill_async()


# ---------- weekly rate refresh -------------------------------------------

def refresh_tariff_rates() -> int:
    """Store today's + tomorrow's unit-rate PERIODS (and standing charges) for
    import / export / gas in tariff_rates. Returns rows written.

    It used to store only the single period covering "now" once a week, so on
    a time-of-use tariff (Cosy's 7 daily windows, Agile's 48) `rate_at(now)`
    almost never matched and the live import rate showed blank. Run daily by
    the master scheduler (and the cron script); idempotent upserts."""
    settings = config.Settings.load()
    if not settings.octopus_configured:
        return 0
    octo = None
    written = 0
    try:
        octo = octopus.OctopusClient(settings.octopus_api_key,
                                     settings.octopus_account_number)
        meters = resolve_meters(settings, octo)
        start = datetime.combine(_today_local(), datetime.min.time(), LONDON)
        pf = start.isoformat()
        # Today + the next two days: the 24 h timeline and the planner's 30 h
        # horizon reach past tomorrow's midnight (fixed tariffs publish ahead;
        # Agile simply returns what's known).
        pt = (start + timedelta(days=3)).isoformat()
        for fuel, tariff in (("electricity_import", meters.import_tariff_code),
                             ("electricity_export", meters.export_tariff_code),
                             ("gas", meters.gas_tariff_code)):
            if not tariff:
                continue
            product, _ = octopus.parse_tariff_code(tariff)
            api_fuel = "gas" if fuel == "gas" else "electricity"
            rates = octo.get_unit_rates(product, tariff, api_fuel, pf, pt)
            standing = octo.get_standing_charge(product, tariff, api_fuel)
            for r in rates:
                if r.get("valid_from") and r.get("value_inc_vat") is not None:
                    db.upsert_tariff_rate(fuel, r["valid_from"],
                                          r["value_inc_vat"], r.get("valid_to"),
                                          standing)
                    written += 1
        try:
            import tariff_watch
            tariff_watch.check(meters)
        except Exception:  # noqa: BLE001 — the watcher must never break the refresh
            log.exception("Tariff watch failed")
    except octopus.OctopusError as e:
        log.warning("Rate refresh failed: %s", e)
    finally:
        if octo is not None:
            octo.close()
    return written
