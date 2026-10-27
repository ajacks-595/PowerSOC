"""Octopus Energy REST API client (electricity import/export + gas).

Auth: HTTP Basic with the API key as the username and an empty password.
Base: https://api.octopus.energy/v1

Design notes / gotchas baked in (see CLAUDE.md):
- Cosy windows are classified by LOCAL time-of-day (Europe/London), never by
  ranking rate values — quarterly price-cap changes can produce >3 distinct
  values in a day and break a rank-based classifier.
- The active tariff agreement is selected by `valid_from <= now < valid_to`,
  not by taking the most recent agreement (a future-dated one may be queued).
- The export MPAN has its OWN meter serial — never reuse the import serial.
- Gas smart-meter consumption is in m³; convert with the standard UK formula.
- Standing charges are p/day (added once per daily summary, not per interval).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from dateutil import parser as dtparser
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

log = logging.getLogger("powersoc.octopus")

BASE_URL = "https://api.octopus.energy/v1"
HTTP_TIMEOUT = 30
LONDON = ZoneInfo("Europe/London")

# UK gas volume->energy conversion (matches Octopus bills):
#   kWh = m3 * volume_correction * calorific_value / 3.6
GAS_VOLUME_CORRECTION = 1.02264
GAS_CALORIFIC_VALUE = 39.5    # MJ/m3
_PARTIAL_THRESHOLD = 40       # < this many of 48 -> mark the day partial


# ---------- exceptions ----------------------------------------------------

class OctopusError(Exception):
    pass


class OctopusConnectionError(OctopusError):
    """Network-level failure (DNS, refused, timeout), wrapped at the client
    boundary so callers can catch OctopusError instead of raw requests
    exceptions (which previously escaped untyped, 500'd API routes and could
    abort the daily sync)."""


class _HTTPStatusError(OctopusError):
    def __init__(self, status: int, msg: str = "", retry_after: float | None = None):
        self.status = status
        self.retry_after = retry_after
        super().__init__(f"HTTP {status}: {msg}")


def _retry_after_seconds(r) -> float | None:
    """Parse a numeric Retry-After header (HTTP-date form is ignored)."""
    v = r.headers.get("Retry-After")
    try:
        return float(v) if v else None
    except (TypeError, ValueError):
        return None


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, OctopusConnectionError):
        return True
    if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
        return True
    if isinstance(exc, _HTTPStatusError):
        return exc.status >= 500 or exc.status == 429
    return False


_BACKOFF = wait_exponential(min=2, max=30)


def _wait_honouring_retry_after(retry_state):
    """Backoff that honours an upstream Retry-After (capped) when present —
    blind exponential backoff can land every retry inside the same 429 window."""
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    if isinstance(exc, _HTTPStatusError) and exc.retry_after:
        return min(exc.retry_after, 60.0)
    return _BACKOFF(retry_state)


# ---------- pure helpers (unit-tested, no HTTP) ---------------------------

def to_london(dt_or_str) -> datetime:
    """Parse an Octopus timestamp (offset-aware ISO) and convert to
    Europe/London. Naive inputs are assumed UTC."""
    dt = dt_or_str if isinstance(dt_or_str, datetime) else dtparser.parse(dt_or_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(LONDON)


def cosy_window(ts_local: datetime) -> str:
    """Classify a local timestamp into a Cosy Octopus tariff window.

    Cheap: 04:00-07:00, 13:00-16:00, 22:00-24:00. Peak: 16:00-19:00. Day: rest.
    """
    h = ts_local.hour + ts_local.minute / 60.0
    if 4 <= h < 7 or 13 <= h < 16 or 22 <= h < 24:
        return "cheap"
    if 16 <= h < 19:
        return "peak"
    return "day"


def is_cosy_product(code: str | None) -> bool:
    """Whether a tariff/product code is a (fixed-window) Cosy Octopus tariff."""
    return bool(code) and "COSY" in code.upper()


def _price_band(rate: float, day_rates: list[float]) -> str:
    """Band `rate` by where it sits in the day's price RANGE: bottom third ->
    cheap, top third -> peak, else day. A two-level day (Go, Intelligent) is
    cheap/day; a ~flat day is all 'day'.

    It used to take terciles of the list of rate PERIODS, so a tariff with one
    long cheap period among a few dear ones (Go: [27, 7.5, 27]) put both
    cut-offs on the dear price and banded everything 'day'."""
    vals = sorted({round(v, 4) for v in day_rates if v is not None})
    if len(vals) < 2 or vals[-1] - vals[0] < 0.5:     # flat (or ~flat)
        return "day"
    lo, hi = vals[0], vals[-1]
    if len(vals) == 2:
        return "cheap" if rate <= lo + 1e-9 else "day"
    third = (hi - lo) / 3
    if rate <= lo + third:
        return "cheap"
    if rate >= hi - third:
        return "peak"
    return "day"


def classify_window(ts_local: datetime, rate: float | None = None,
                    day_rates: list[float] | None = None,
                    tariff_code: str | None = None) -> str:
    """Bucket an interval into cheap|day|peak for the import breakdown.

    Cosy (and unknown) tariffs use fixed Europe/London time bands
    (`cosy_window`) — ranking is deliberately NOT used for Cosy, since
    price-cap changes can yield >3 values/day (see CLAUDE.md gotcha). Other
    tariffs (Agile/Go/Flux/flat) have no fixed windows, so classify by the
    interval's price within that day's spread. Either way cost is unchanged:
    it is always consumption × the interval's own rate."""
    if tariff_code is None or is_cosy_product(tariff_code):
        return cosy_window(ts_local)
    if rate is None or not day_rates:
        return "day"
    return _price_band(rate, day_rates)


def m3_to_kwh(m3: float) -> float:
    """Convert gas volume (m³) to energy (kWh) using the standard UK formula."""
    return m3 * GAS_VOLUME_CORRECTION * GAS_CALORIFIC_VALUE / 3.6


def parse_tariff_code(tariff_code: str) -> tuple[str, str]:
    """Split a full tariff code into (product_code, region_letter).

    e.g. 'E-1R-COSY-22-12-08-A' -> ('COSY-22-12-08', 'A').
    Format is '{fuel}-{rate-type}-{PRODUCT...}-{REGION}'. The product can itself
    contain hyphens, so we strip the two leading segments and the trailing
    single-letter region."""
    if not tariff_code:
        raise OctopusError("empty tariff code")
    parts = tariff_code.split("-")
    if len(parts) < 4:
        raise OctopusError(f"unrecognised tariff code: {tariff_code}")
    region = parts[-1]
    product = "-".join(parts[2:-1])
    return product, region


def select_active_agreement(agreements: list[dict],
                            now: datetime | None = None) -> dict | None:
    """Return the agreement valid right now: valid_from <= now < valid_to.

    `valid_to` of None means open-ended. Picks the latest-starting match if
    several overlap. Returns None when none are currently active."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    def _aware(v):
        if not v:
            return None
        d = dtparser.parse(v)
        return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d

    active = []
    for a in agreements or []:
        vf = _aware(a.get("valid_from"))
        vt = _aware(a.get("valid_to"))
        if vf and vf > now:
            continue            # future-dated — not active yet
        if vt and vt <= now:
            continue            # already expired
        active.append((vf or datetime.min.replace(tzinfo=timezone.utc), a))
    if not active:
        return None
    active.sort(key=lambda p: p[0])
    return active[-1][1]


def prefer_direct_debit(rows: list[dict]) -> list[dict]:
    """Octopus can list the same period twice — DIRECT_DEBIT and
    NON_DIRECT_DEBIT prices. When both exist keep the Direct Debit rows (how
    almost every account pays) instead of letting list order decide."""
    if any((r or {}).get("payment_method") == "DIRECT_DEBIT" for r in rows or []):
        return [r for r in rows if r.get("payment_method") != "NON_DIRECT_DEBIT"]
    return list(rows or [])


def rate_for(ts, rates: list[dict]) -> dict | None:
    """Find the rate period covering timestamp `ts` (offset-aware).

    Each rate dict has `valid_from`, optional `valid_to`, and `value_inc_vat`.
    Returns the matching rate dict, or None."""
    ts = ts if isinstance(ts, datetime) else dtparser.parse(ts)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)

    def _aware(v):
        if not v:
            return None
        d = dtparser.parse(v)
        return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d

    best = None
    best_vf = None
    for r in rates or []:
        vf = _aware(r.get("valid_from"))
        vt = _aware(r.get("valid_to"))
        if vf and vf > ts:
            continue
        if vt and vt <= ts:
            continue
        if best_vf is None or (vf and vf > best_vf):
            best, best_vf = r, vf
    return best


# ---------- result containers ---------------------------------------------

@dataclass
class MeterInfo:
    import_mpan: str = ""
    import_serial: str = ""
    import_tariff_code: str = ""
    export_mpan: str = ""
    export_serial: str = ""
    export_tariff_code: str = ""
    mprn: str = ""
    gas_serial: str = ""
    gas_tariff_code: str = ""
    region: str = ""
    # Full agreement history per kind ("import"/"export"/"gas"): a list of
    # {tariff_code, valid_from, valid_to}. The *_tariff_code fields are only
    # TODAY's tariff — pricing a past day with them put a backfill of
    # history on the current tariff (2026-10 review, P1).
    agreements: dict = field(default_factory=dict)

    def tariff_windows(self, kind: str) -> list[tuple[float, float, str]]:
        """[(start_epoch, end_epoch, tariff_code)] for `kind`, oldest first.
        With no history (explicit settings, an older cache) today's code is
        the best we know and covers all time."""
        history = (self.agreements or {}).get(kind) or []
        if not history:
            code = getattr(self, f"{kind}_tariff_code", "") or ""
            return [(float("-inf"), float("inf"), code)] if code else []
        out = []
        for a in history:
            start = _aware_epoch(a.get("valid_from"), float("-inf"))
            end = _aware_epoch(a.get("valid_to"), float("inf"))
            if a.get("tariff_code") and end > start:   # skip zero-length rows
                out.append((start, end, a["tariff_code"]))
        return sorted(out)

    def tariff_at(self, kind: str, epoch: float) -> str:
        """Tariff in force at an instant (latest-starting match wins)."""
        best = ""
        for start, end, code in self.tariff_windows(kind):
            if start <= epoch < end:
                best = code
        return best

    def tariff_on(self, kind: str, date: str) -> str:
        """Tariff in force on a local day (judged at local noon — agreements
        change at local midnight). "" when no agreement covers the day."""
        noon = datetime.fromisoformat(date).replace(hour=12, tzinfo=LONDON)
        return self.tariff_at(kind, noon.timestamp())


def _aware_epoch(v, default: float) -> float:
    if not v:
        return default
    d = v if isinstance(v, datetime) else dtparser.parse(v)
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.timestamp()


@dataclass
class ConsumptionDay:
    date: str
    intervals: list[dict] = field(default_factory=list)
    count: int = 0
    partial: bool = False


# ---------- client --------------------------------------------------------

class OctopusClient:
    def __init__(self, api_key: str, account_number: str = ""):
        if not api_key:
            raise OctopusError("Octopus API key not configured")
        self.api_key = api_key
        self.account_number = account_number
        self.session = requests.Session()
        self.session.auth = (api_key, "")
        self.session.headers["User-Agent"] = "PowerSOC/1.0"
        # Standing-charge history is cached per (product, tariff, fuel) so a
        # multi-day backfill fetches each tariff's history once, not per day.
        self._standing_cache: dict = {}

    @retry(stop=stop_after_attempt(4),
           wait=_wait_honouring_retry_after,
           retry=retry_if_exception(_is_retryable),
           reraise=True)
    def _get(self, path: str, params: dict | None = None) -> dict:
        url = path if path.startswith("http") else f"{BASE_URL}{path}"
        try:
            r = self.session.get(url, params=params, timeout=HTTP_TIMEOUT)
            if r.status_code >= 400:
                raise _HTTPStatusError(r.status_code, r.text[:200],
                                       retry_after=_retry_after_seconds(r))
            return r.json()
        except (requests.ConnectionError, requests.Timeout) as e:
            raise OctopusConnectionError(
                f"network error: {type(e).__name__}") from e
        except requests.RequestException as e:   # incl. JSON decode of a 200
            raise OctopusError(f"bad response: {type(e).__name__}") from e

    def _get_paginated(self, path: str, params: dict | None = None,
                       max_pages: int = 100) -> list[dict]:
        """Follow `next` links, accumulating `results`. Octopus caps page_size
        at 1500; we let the caller set it and just walk the cursor."""
        results: list[dict] = []
        body = self._get(path, params)
        results.extend(body.get("results", []) or [])
        pages = 1
        while body.get("next") and pages < max_pages:
            nxt = str(body["next"])
            # The session carries the API key as Basic auth on EVERY request:
            # only follow cursors that stay on the Octopus API origin.
            if not nxt.startswith(BASE_URL.rsplit("/v1", 1)[0] + "/"):
                log.warning("Ignoring off-origin pagination link for %s", path)
                break
            body = self._get(nxt)
            results.extend(body.get("results", []) or [])
            pages += 1
        if body.get("next"):
            log.warning("Pagination truncated at %d pages for %s — results "
                        "incomplete", max_pages, path)
        return results

    # ----- discovery -----

    def get_account(self) -> dict:
        if not self.account_number:
            raise OctopusError("Octopus account number not configured")
        return self._get(f"/accounts/{self.account_number}/")

    def _has_recent_data(self, base_path: str,
                         now: datetime | None = None) -> bool:
        """ONE request: any consumption in the last 10 days (covers the
        smart-meter settlement lag). Replaces probing three separate dates."""
        end = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        body = self._get(base_path + "consumption/", {
            "period_from": (end - timedelta(days=10)).isoformat(),
            "period_to": end.isoformat(), "page_size": 1})
        return bool(body.get("count") or body.get("results"))

    def _select_serial(self, meters: list[dict], has_data) -> str:
        """Choose the live meter serial. A meter point can list a decommissioned
        meter alongside the current one; with several, pick the one that returns
        recent consumption, else fall back to the last (usually newest) serial.
        Probed newest-first, so the common case costs a single request."""
        serials = [m.get("serial_number") for m in (meters or [])
                   if m.get("serial_number")]
        if len(serials) <= 1:
            return serials[0] if serials else ""
        for s in reversed(serials):
            try:
                if has_data(s):
                    return s
            except OctopusError:
                pass
        return serials[-1]

    def discover_meters(self, now: datetime | None = None) -> MeterInfo:
        """Parse the account structure into import/export/gas meter details and
        their active tariff codes. Import vs export distinguished by the
        meter point's `is_export` flag; each has its own serial. When a point
        lists multiple meters, the one with recent data is selected."""
        acct = self.get_account()
        props = acct.get("properties") or [{}]
        # Multi-property accounts (house moves) keep old addresses in the list
        # with no guaranteed order — prefer the one still occupied
        # (moved_out_at is null) so we don't bind decommissioned meters.
        current = [p for p in props if not p.get("moved_out_at")]
        prop = (current or props)[0]
        if len(props) > 1:
            log.info("Account has %d properties; using %s", len(props),
                     prop.get("address_line_1") or prop.get("id") or "first")
        info = MeterInfo()

        def _history(agreements):
            return [{"tariff_code": a.get("tariff_code", ""),
                     "valid_from": a.get("valid_from"),
                     "valid_to": a.get("valid_to")}
                    for a in agreements or [] if a.get("tariff_code")]

        for mp in prop.get("electricity_meter_points", []) or []:
            mpan = mp.get("mpan", "")
            meters = mp.get("meters") or []
            agreement = select_active_agreement(mp.get("agreements", []), now)
            tariff = agreement.get("tariff_code", "") if agreement else ""
            def _probe(s, mpan=mpan):
                return self._has_recent_data(
                    f"/electricity-meter-points/{mpan}/meters/{s}/", now)
            if mp.get("is_export"):
                serial = self._select_serial(meters, _probe)
                info.export_mpan, info.export_serial = mpan, serial
                info.export_tariff_code = tariff
                info.agreements["export"] = _history(mp.get("agreements"))
            else:
                serial = self._select_serial(meters, _probe)
                info.import_mpan, info.import_serial = mpan, serial
                info.import_tariff_code = tariff
                info.agreements["import"] = _history(mp.get("agreements"))
                if tariff and not info.region:
                    _, info.region = parse_tariff_code(tariff)

        for gp in prop.get("gas_meter_points", []) or []:
            mprn = gp.get("mprn", "")
            info.mprn = mprn
            info.gas_serial = self._select_serial(
                gp.get("meters") or [], lambda s, mprn=mprn: self._has_recent_data(
                    f"/gas-meter-points/{mprn}/meters/{s}/", now))
            agreement = select_active_agreement(gp.get("agreements", []), now)
            if agreement:
                info.gas_tariff_code = agreement.get("tariff_code", "")
            info.agreements["gas"] = _history(gp.get("agreements"))
            break

        return info

    # ----- consumption -----

    @staticmethod
    def day_bounds(date: str) -> tuple[str, str]:
        """period_from/period_to spanning one local day (midnight to next
        midnight, Europe/London), as offset-aware ISO strings."""
        start = datetime.fromisoformat(date).replace(tzinfo=LONDON)
        end = start + timedelta(days=1)
        return start.isoformat(), end.isoformat()

    def _consumption(self, base_path: str, date: str) -> list[dict]:
        """One local day's readings. Octopus treats period_to inclusively, so
        the reply also carried the NEXT day's 00:00 reading: priced against
        rates ending at midnight it was stored unpriced, and a newest-first
        backfill overwrote that day's correctly priced row with it (one NULL
        00:00 interval on 169 live days). Rows outside the day are dropped."""
        pf, pt = self.day_bounds(date)
        rows = self._get_paginated(base_path + "consumption/", {
            "period_from": pf, "period_to": pt,
            "page_size": 100, "order_by": "period",
        })
        return [r for r in rows
                if to_london(r["interval_start"]).date().isoformat() == date]

    def fetch_electricity_import(self, mpan: str, serial: str, date: str,
                                 rates: list[dict] | None = None,
                                 tariff_code: str | None = None) -> ConsumptionDay:
        rows = self._consumption(
            f"/electricity-meter-points/{mpan}/meters/{serial}/", date)
        day_rates = [r.get("value_inc_vat") for r in (rates or [])]
        out = []
        for r in rows:
            ts = to_london(r["interval_start"])
            kwh = float(r["consumption"])
            rate = rate_for(r["interval_start"], rates) if rates else None
            p = rate.get("value_inc_vat") if rate else None
            out.append({
                "interval_start": ts.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "interval_end": to_london(r["interval_end"]).strftime("%Y-%m-%dT%H:%M:%S%z"),
                "consumption_kwh": kwh,
                "tariff_window": classify_window(ts, p, day_rates, tariff_code),
                "rate_p_per_kwh": p,
                "cost_pence": round(kwh * p, 4) if p is not None else None,
            })
        return ConsumptionDay(date, out, len(out), len(out) < _PARTIAL_THRESHOLD)

    def fetch_electricity_export(self, mpan: str, serial: str, date: str,
                                 rates: list[dict] | None = None) -> ConsumptionDay:
        rows = self._consumption(
            f"/electricity-meter-points/{mpan}/meters/{serial}/", date)
        out = []
        for r in rows:
            ts = to_london(r["interval_start"])
            kwh = float(r["consumption"])
            rate = rate_for(r["interval_start"], rates) if rates else None
            p = rate.get("value_inc_vat") if rate else None
            out.append({
                "interval_start": ts.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "interval_end": to_london(r["interval_end"]).strftime("%Y-%m-%dT%H:%M:%S%z"),
                "export_kwh": kwh,
                "rate_p_per_kwh": p,
                "earned_pence": round(kwh * p, 4) if p is not None else None,
            })
        return ConsumptionDay(date, out, len(out), len(out) < _PARTIAL_THRESHOLD)

    def fetch_gas(self, mprn: str, serial: str, date: str,
                  rates: list[dict] | None = None,
                  units: str = "m3") -> ConsumptionDay:
        """Fetch a day of gas. `units='m3'` (default, per spec) converts to kWh;
        pass 'kwh' if the meter already reports kWh to avoid double-conversion."""
        rows = self._consumption(
            f"/gas-meter-points/{mprn}/meters/{serial}/", date)
        out = []
        for r in rows:
            ts = to_london(r["interval_start"])
            raw = float(r["consumption"])
            kwh = m3_to_kwh(raw) if units == "m3" else raw
            rate = rate_for(r["interval_start"], rates) if rates else None
            p = rate.get("value_inc_vat") if rate else None
            out.append({
                "interval_start": ts.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "interval_end": to_london(r["interval_end"]).strftime("%Y-%m-%dT%H:%M:%S%z"),
                "consumption_kwh": round(kwh, 5),
                "rate_p_per_kwh": p,
                "cost_pence": round(kwh * p, 4) if p is not None else None,
            })
        return ConsumptionDay(date, out, len(out), len(out) < _PARTIAL_THRESHOLD)

    # ----- rates & standing charges -----

    def get_unit_rates(self, product: str, tariff_code: str, fuel: str,
                       period_from: str, period_to: str) -> list[dict]:
        seg = "gas-tariffs" if fuel == "gas" else "electricity-tariffs"
        return prefer_direct_debit(self._get_paginated(
            f"/products/{product}/{seg}/{tariff_code}/standard-unit-rates/",
            {"period_from": period_from, "period_to": period_to, "page_size": 1500}))

    def get_standing_charges(self, product: str, tariff_code: str,
                             fuel: str) -> list[dict]:
        """Full standing-charge history for a tariff, cached per client so a
        backfill doesn't refetch the same history for every day."""
        key = (product, tariff_code, fuel)
        if key not in self._standing_cache:
            seg = "gas-tariffs" if fuel == "gas" else "electricity-tariffs"
            self._standing_cache[key] = prefer_direct_debit(self._get_paginated(
                f"/products/{product}/{seg}/{tariff_code}/standing-charges/",
                {"page_size": 100}))
        return self._standing_cache[key]

    def get_standing_charge(self, product: str, tariff_code: str, fuel: str,
                            at: datetime | None = None) -> float | None:
        """Standing charge (p/day) in force at `at` (default: now). Historical
        days MUST pass `at` so a backfill reconstructs the charge that actually
        applied on that day, not today's."""
        rows = self.get_standing_charges(product, tariff_code, fuel)
        row = rate_for(at or datetime.now(timezone.utc), rows)
        if row is None and at is None and rows:
            row = rows[0]       # "now": the newest charge is the best guess
        # A past date the tariff doesn't cover gets None — never today's charge.
        return row.get("value_inc_vat") if row else None

    def get_flexible_import_rate(self, region: str,
                                 at: datetime | None = None) -> float | None:
        """Current Flexible Octopus import unit rate for a region — the
        counterfactual flat rate for the 'saving vs Flexible' calculation."""
        params = {"brand": "OCTOPUS_ENERGY", "is_variable": "true"}
        if at is not None:
            # The Flexible product code changes over time: list the products
            # on sale THEN, or a past day compares against today's price.
            params["available_at"] = at.astimezone(timezone.utc).isoformat()
        products = self._get_paginated("/products/", params)
        product_code = None
        for p in products:
            if (p.get("display_name") == "Flexible Octopus"
                    and p.get("direction", "IMPORT") == "IMPORT"):
                product_code = p.get("code")
                break
        if not product_code:
            return None
        tariff_code = f"E-1R-{product_code}-{region}"
        at = at or datetime.now(timezone.utc)
        rates = self.get_unit_rates(
            product_code, tariff_code, "electricity",
            (at - timedelta(hours=2)).isoformat(), (at + timedelta(hours=2)).isoformat())
        row = rate_for(at, rates) or (rates[0] if rates else None)
        return row.get("value_inc_vat") if row else None

    def close(self) -> None:
        self.session.close()
