"""Sunsynk Connect API client (synchronous, requests + tenacity).

Why this is hand-rolled rather than using `sunsynk-api-client`
--------------------------------------------------------------
The PyPI library (`sunsynk-api-client` v1.0.9) was read during the pre-build
verification step. Findings that shaped this module:

* Its login uses the **modern** scheme: `POST /oauth/token/new` with the
  password RSA-PKCS1v15-encrypted under a public key from
  `GET /anonymous/publicKey`, plus a `nonce` and an MD5 `sign`. The plaintext
  `POST /oauth/token` flow documented in older community code is NOT what the
  maintained library does.
* It is async (aiohttp) and exposes only `plants`, `inverters`, and
  `/realtime/*` endpoints — **no `/day` time-series endpoints at all**.

PowerSOC needs (a) a synchronous client and (b) intraday series + historical
backfill, which the realtime endpoints cannot provide. So this module:

* Implements login with the modern RSA flow as primary and the legacy
  plaintext flow as an automatic fallback (whichever the account accepts).
* Implements the `/day` series endpoints documented in the build spec and
  integrates the power series into kWh, resampled to half-hourly buckets.
* Cross-checks against the realtime scalars (`etoday`, `etodayChg`, …) which
  are verified-present from the library's resource classes.

CAVEAT (mock-only build): the exact `/day` JSON shape and the live-accepted
auth flow could not be confirmed against a live inverter. The series parser is
deliberately tolerant; verify field names against a real response and adjust
`_SERIES_MATCHERS` / `_DAY_*` constants if numbers look wrong. See CLAUDE.md
gotcha #1.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from datetime import timedelta as _timedelta
from zoneinfo import ZoneInfo

import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding
from dateutil import parser as dtparser
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

import config

log = logging.getLogger("powersoc.sunsynk")

BASE_URL = os.environ.get("SUNSYNK_BASE_URL", "https://api.sunsynk.net")
_SOURCE = "sunsynk"
_CLIENT_ID = "csp-web"
HTTP_TIMEOUT = 20


# ---------- exceptions ----------------------------------------------------

class SunsynkError(Exception):
    """Base for Sunsynk client errors."""


class SunsynkAuthError(SunsynkError):
    """Login DEFINITIVELY rejected (bad credentials / auth flow refused).
    Transient failures (network, 5xx, 429) are never this type."""


class SunsynkConnectionError(SunsynkError):
    """Network-level failure (DNS, refused, timeout), wrapped at the client
    boundary so callers can catch SunsynkError instead of raw requests
    exceptions (which previously escaped untyped and 500'd the API routes)."""


class _HTTPStatusError(SunsynkError):
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
    """Retry transient failures only. 401/403/404 are config/auth problems."""
    if isinstance(exc, SunsynkConnectionError):
        return True
    if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
        return True
    if isinstance(exc, _HTTPStatusError):
        return exc.status >= 500 or exc.status == 429
    return False


_BACKOFF = wait_exponential(min=2, max=30)


def _wait_honouring_retry_after(retry_state):
    """Backoff that honours an upstream Retry-After (capped) when present."""
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    if isinstance(exc, _HTTPStatusError) and exc.retry_after:
        return min(exc.retry_after, 60.0)
    return _BACKOFF(retry_state)


# ---------- series maths (pure, unit-tested) ------------------------------

def _parse_time(value) -> datetime:
    """Parse a Sunsynk record time. Accepts ISO strings, 'HH:MM' / 'HH:MM:SS'
    clock strings (relative to a date supplied by the caller elsewhere), and
    epoch-ish numbers. Returns a naive datetime; only deltas are used."""
    if isinstance(value, (int, float)):
        # Heuristic: ms epoch if large, else seconds. Aware (UTC): a naive
        # host-local conversion shifted every record by the host's zone.
        ts = value / 1000.0 if value > 1e11 else float(value)
        return datetime.fromtimestamp(ts, timezone.utc)
    s = str(value).strip()
    # Bare clock time like "06:30" — anchor to an arbitrary fixed date; only
    # inter-record deltas matter for integration.
    if len(s) <= 8 and s.count(":") in (1, 2) and "-" not in s and "T" not in s:
        parts = [int(p) for p in s.split(":")]
        while len(parts) < 3:
            parts.append(0)
        return datetime(2000, 1, 1, parts[0], parts[1], parts[2])
    return dtparser.parse(s)


def _is_fall_back_day(date: str | None) -> bool:
    """True on the autumn Europe/London DST day (25 local hours)."""
    if not date:
        return False
    try:
        from zoneinfo import ZoneInfo
        lon = ZoneInfo("Europe/London")
        start = datetime.fromisoformat(date).replace(tzinfo=lon)
        end = (start + _timedelta(days=1)).replace(tzinfo=lon)
        return end.timestamp() - start.timestamp() > 24 * 3600
    except ValueError:
        return False


def _records_xy(records, keep_order: bool = False) -> list[tuple[datetime, float]]:
    """Normalise a list of {time/value} records to (datetime, float), sorted.

    Tolerates the common key spellings seen across Sunsynk responses
    (time/date/x, value/val/y) and skips records missing either part.
    `keep_order` (autumn DST day): clock-only times repeat 01:00–02:00, and
    sorting interleaved the two passes so one hour of energy vanished; the
    API's own chronological order is kept instead and the single backwards
    step is skipped by the integrators (dt <= 0)."""
    out: list[tuple[datetime, float]] = []
    for r in records or []:
        t = r.get("time") if isinstance(r, dict) else None
        if t is None and isinstance(r, dict):
            t = r.get("date") or r.get("x") or r.get("dateTime")
        v = None
        if isinstance(r, dict):
            for k in ("value", "val", "y", "power"):
                if r.get(k) is not None:
                    v = r[k]
                    break
        if t is None or v is None:
            continue
        try:
            out.append((_parse_time(t), float(v)))
        except (ValueError, TypeError):
            continue
    if not keep_order:
        out.sort(key=lambda p: p[0])
    return out


def _power_to_kw(value: float, unit: str | None) -> float:
    u = (unit or "").strip().lower()
    if u in ("w", "watt", "watts"):
        return value / 1000.0
    # kW (or unknown — Sunsynk power series are typically kW for plant/day).
    return value


_LONDON = ZoneInfo("Europe/London")
_SLOT_S = 1800   # UK offsets are whole hours: local half-hours align with epoch


def _timeline(records, date: str | None) -> list[tuple[float, float]]:
    """(real epoch seconds, value) per record, in chronological order.

    Sunsynk reports inverter-local CLOCK times ("00:55", "02:00"). With the
    `date`, each is anchored to Europe/London so deltas are real elapsed time:
    on the spring-forward day 00:55 -> 02:00 is five minutes, not 65 (the
    wall-clock delta over-counted that day by ~4%: the 2026-10-07 rule); on the
    autumn day the clock steps back an hour and the second pass gets fold=1.
    Without a date (older callers) deltas are wall-clock, as before."""
    fall = _is_fall_back_day(date)
    xy = _records_xy(records, keep_order=fall)
    if not date:
        epoch = datetime(1970, 1, 1)
        return [((t.replace(tzinfo=None) - epoch).total_seconds()
                 if t.tzinfo is None else t.timestamp(), v) for t, v in xy]
    base = datetime.fromisoformat(date)
    out: list[tuple[float, float]] = []
    fold, prev = 0, None
    for t, v in xy:
        if t.tzinfo is not None:
            out.append((t.timestamp(), v))
            continue
        local = (base.replace(hour=t.hour, minute=t.minute, second=t.second)
                 if t.year == 2000 else t)       # 2000 = clock-only placeholder
        if fall and prev is not None and local < prev:
            fold = 1
        prev = local
        out.append((local.replace(tzinfo=_LONDON, fold=fold).timestamp(), v))
    return out


def _segments(records, date: str | None, unit: str | None):
    """Trapezoid segments as (start_epoch, end_epoch, average kW)."""
    pts = _timeline(records, date)
    for (e0, p0), (e1, p1) in zip(pts, pts[1:]):
        if e1 > e0:
            yield e0, e1, (_power_to_kw(p0, unit) + _power_to_kw(p1, unit)) / 2.0


def integrate_series_kwh(records, unit: str | None = "kW",
                         date: str | None = None) -> float:
    """Trapezoidal integration of a power series to energy (kWh) over REAL
    elapsed time (pass `date`: see _timeline). 0.0 for <2 records."""
    total = sum(kw * (e1 - e0) / 3600.0 for e0, e1, kw in _segments(records, date, unit))
    return round(total, 4)


def _spread(e0: float, e1: float, kwh_per_h: float, date: str,
            into: dict[str, float]) -> None:
    """Add a segment's energy to the local half-hour buckets it overlaps, in
    proportion to time. A gap between records used to put ALL its energy in
    the half-hour where it started. Energy past the day's end is dropped."""
    t = e0
    while t < e1:
        end = min(e1, (t // _SLOT_S + 1) * _SLOT_S)
        local = datetime.fromtimestamp(t, _LONDON)
        if local.date().isoformat() == date:
            key = local.replace(minute=0 if local.minute < 30 else 30, second=0,
                                microsecond=0).strftime("%Y-%m-%dT%H:%M")
            into[key] = into.get(key, 0.0) + kwh_per_h * (end - t) / 3600.0
        t = end


def resample_halfhourly_energy(records, date: str, unit: str | None = "kW"
                               ) -> dict[str, float]:
    """Bucket a power series into half-hourly kWh totals for a local date:
    {interval_start 'YYYY-MM-DDTHH:MM' (local): kWh}. Each trapezoid segment
    is split across the half-hours it spans by real elapsed time, so the
    buckets sum to the daily integral (46/48/50 buckets on DST days, the
    autumn repeated hour landing in the same labels)."""
    buckets: dict[str, float] = {}
    for e0, e1, kw in _segments(records, date, unit):
        _spread(e0, e1, kw, date, buckets)
    return {k: round(v, 4) for k, v in buckets.items()}


def integrate_signed_kwh(records, unit: str | None = "kW",
                         date: str | None = None) -> tuple[float, float]:
    """Integrate a SIGNED power series into (positive_kwh, abs_negative_kwh).

    Sunsynk's plant /day returns one signed Battery series (+charge/-discharge)
    and one signed Grid series (+import/-export); this splits the energy by the
    sign of each trapezoidal segment."""
    pos = neg = 0.0
    for e0, e1, kw in _segments(records, date, unit):
        e = kw * (e1 - e0) / 3600.0
        if e >= 0:
            pos += e
        else:
            neg -= e
    return round(pos, 4), round(neg, 4)


def resample_halfhourly_signed(records, date: str, unit: str | None = "kW"
                               ) -> tuple[dict[str, float], dict[str, float]]:
    """Half-hourly buckets of a signed series, split into (positive, abs-negative)
    kWh dicts keyed by interval_start."""
    pos: dict[str, float] = {}
    neg: dict[str, float] = {}
    for e0, e1, kw in _segments(records, date, unit):
        if kw >= 0:
            _spread(e0, e1, kw, date, pos)
        else:
            _spread(e0, e1, -kw, date, neg)
    return ({k: round(v, 4) for k, v in pos.items()},
            {k: round(v, 4) for k, v in neg.items()})


def battery_sign(battery_records, soc_records, date: str,
                 unit: str | None = "kW") -> int:
    """+1 if the signed Battery series is +charge/−discharge, −1 if it is
    +discharge/−charge — read from the day's own SoC: over each stretch
    between SoC readings that moved a point or more, does the series' net
    energy have the sign of the SoC change? The code used to ASSUME +charge;
    the live inverter reports +discharge (2026-10-27), which swapped charge
    and discharge on every stored day. Without SoC evidence: −1 (live)."""
    segs = list(_segments(battery_records, date, unit))
    soc = _timeline(soc_records, date)
    votes = 0
    if not soc:
        return -1
    t0, s0 = soc[0]
    for t1, s1 in soc[1:]:
        d = s1 - s0
        if abs(d) < 1 or t1 <= t0:
            continue                 # SoC moves < 1 point per 5-min reading
        e = sum(kw * (min(e1, t1) - max(e0, t0)) / 3600.0
                for e0, e1, kw in segs if e1 > t0 and e0 < t1)
        if abs(e) > 0.02:
            votes += 1 if (d > 0) == (e > 0) else -1
        t0, s0 = t1, s1
    return 1 if votes > 0 else -1


def resample_halfhourly_last(records, date: str) -> dict[str, float]:
    """Bucket a state series (e.g. SoC %) to 48 half-hourly snapshots, taking
    the last record value within each half-hour. Returns {interval_start: val}."""
    xy = _records_xy(records)
    base = datetime.fromisoformat(date)
    out: dict[str, float] = {}
    for t, v in xy:
        if t.tzinfo is not None:
            t = t.astimezone(_LONDON)
        slot_min = 0 if t.minute < 30 else 30
        key = base.replace(hour=t.hour, minute=slot_min, second=0,
                           microsecond=0).strftime("%Y-%m-%dT%H:%M")
        out[key] = v   # later record in the bucket overwrites — i.e. "last"
    return out


def peak_value(records) -> float | None:
    xy = _records_xy(records)
    return max((v for _, v in xy), default=None)


def last_value(records) -> float | None:
    xy = _records_xy(records)
    return xy[-1][1] if xy else None


# ---------- day-fetch result ----------------------------------------------

@dataclass
class DayResult:
    """Result of fetching one day of Sunsynk data."""
    date: str
    intervals: list[dict] = field(default_factory=list)   # solar_intervals rows
    pv_kwh: float = 0.0
    battery_charge_kwh: float = 0.0
    battery_discharge_kwh: float = 0.0
    grid_import_kwh: float = 0.0
    grid_export_kwh: float = 0.0
    house_load_kwh: float = 0.0
    peak_soc_pct: float | None = None
    end_soc_pct: float | None = None
    ok: bool = True
    empty: bool = False    # True when the API returned no records for the day


# How to recognise each energy series in a /day "infos" payload, by label
# substring (lower-cased). Verified against a live inverter (2026): the plant
# energy/day endpoint returns labels PV, Battery, SOC, Load, Grid — where
# Battery and Grid are SINGLE SIGNED series (see sign convention below), not
# separate charge/discharge or import/export. The explicit *_charge/_discharge/
# _import/_export matchers are kept first so any inverter that DOES split them
# is still handled; the bare "battery"/"grid" matchers catch the signed form.
_SERIES_MATCHERS = {
    "pv":        ("pv", "solar", "generation"),
    # "discharge" MUST be checked before "charge": "charge" is a substring of
    # "discharge" and _classify_series takes the first match in dict order, so
    # the reverse order mislabels a split "Battery Discharge" series as charge.
    "battery_discharge": ("discharge",),
    "battery_charge":    ("charge",),
    "grid_import":  ("import", "buy", "from grid", "grid consumption"),
    "grid_export":  ("export", "sell", "to grid", "feed"),
    "load":      ("load", "consumption", "use"),
    "soc":       ("soc", "state of charge"),
    "battery":   ("battery",),   # signed: + = charge, - = discharge
    "grid":      ("grid",),      # signed: + = import, - = export
}

_DAY_ENERGY_PATH = "api/v1/plant/energy/{plant_id}/day"


class SunsynkClient:
    """Synchronous Sunsynk Connect client.

    Build with `SunsynkClient(settings)` where settings is a config.Settings (or
    anything exposing the sunsynk_* attributes). Call `login()` (cached token is
    reused within its lifetime), then `fetch_day(date)`.
    """

    def __init__(self, settings=None, *, username: str | None = None,
                 password: str | None = None, plant_id: str | None = None,
                 inverter_sn: str | None = None,
                 token_cache_path: str | None = None):
        if settings is not None:
            username = username or settings.sunsynk_username
            password = password or settings.sunsynk_password
            plant_id = plant_id or settings.sunsynk_plant_id
            inverter_sn = inverter_sn or settings.sunsynk_inverter_sn
        self.username = username or ""
        self.password = password or ""
        self.plant_id = plant_id or ""
        self.inverter_sn = inverter_sn or ""
        self._token: str | None = None
        self._refresh_token: str | None = None
        self._token_expiry: float = 0.0
        self.token_cache_path = token_cache_path or config.TOKEN_CACHE_PATH
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "PowerSOC/1.0"
        # Serialises login so concurrent realtime fetches re-login at most once.
        self._login_lock = threading.Lock()

    # ----- token cache -----

    def _load_cached_token(self) -> bool:
        try:
            with open(self.token_cache_path) as f:
                data = json.load(f)
        except (OSError, ValueError):
            return False
        if data.get("username") != self.username:
            return False
        if data.get("expiry", 0) <= time.time() + 60:
            return False
        self._token = data.get("access_token")
        self._refresh_token = data.get("refresh_token")
        self._token_expiry = data.get("expiry", 0)
        return bool(self._token)

    def _save_cached_token(self) -> None:
        """Persist the token for other processes (web vs cron). Best-effort:
        a full or read-only disk used to raise OSError out of login() — past
        every `except SunsynkError` — and abort the daily sync. The token is
        still used in memory. The temp file is unique per write: two
        processes re-logging in together raced on one fixed ".tmp" name."""
        try:
            config.ensure_dirs()
            folder = os.path.dirname(self.token_cache_path) or "."
            # mkstemp creates the file 0600 — the bearer token is never
            # world-readable, even between write and rename.
            fd, tmp = tempfile.mkstemp(dir=folder, prefix=".sunsynk_token.",
                                       suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump({
                        "username": self.username,
                        "access_token": self._token,
                        "refresh_token": self._refresh_token,
                        "expiry": self._token_expiry,
                    }, f)
                os.replace(tmp, self.token_cache_path)
            except BaseException:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                raise
        except OSError as e:
            log.warning("Sunsynk token cache not written (%s) — using the token "
                        "in memory only", type(e).__name__)

    def _clear_token(self, if_token: str | None = None) -> None:
        """Drop the in-memory token and the cache file. With `if_token`, the
        file is only deleted while it still holds that (failing) token —
        another process may have just written a fresh one we should keep."""
        self._token = None
        self._token_expiry = 0.0
        if if_token is not None:
            try:
                with open(self.token_cache_path) as f:
                    if json.load(f).get("access_token") != if_token:
                        return
            except (OSError, ValueError):
                pass   # unreadable/corrupt — fall through and delete
        try:
            os.remove(self.token_cache_path)
        except OSError:
            pass

    # ----- auth -----

    @staticmethod
    def _nonce() -> int:
        return int(time.time() * 1000)

    @staticmethod
    def _md5(value: str) -> str:
        # MD5 is mandated by the Sunsynk login protocol's `sign` field — it is
        # NOT used for security here (not a password/credential hash).
        return hashlib.md5(value.encode(), usedforsecurity=False).hexdigest()

    @staticmethod
    def _rsa_encrypt(raw_key: str, plaintext: str) -> str:
        pem = (f"-----BEGIN PUBLIC KEY-----\n{raw_key}\n"
               f"-----END PUBLIC KEY-----").encode()
        public_key = serialization.load_pem_public_key(pem)
        ct = public_key.encrypt(plaintext.encode(), padding.PKCS1v15())
        return base64.b64encode(ct).decode()

    def _store_token_body(self, body: dict) -> None:
        data = body["data"]
        self._token = data["access_token"]
        self._refresh_token = data.get("refresh_token")
        # Tokens last ~6 days; cache conservatively for 5 days.
        self._token_expiry = time.time() + data.get("expires_in", 5 * 86400)
        self._save_cached_token()

    def _login_modern(self) -> None:
        """RSA+nonce+sign flow at /oauth/token/new (matches the live library).

        Raises SunsynkAuthError ONLY on a definitive rejection; transient
        failures (network, 5xx, 429, garbage body) raise other SunsynkError
        types so login() never falls back to the plaintext legacy flow for
        what is merely platform flakiness."""
        try:
            nonce = self._nonce()
            sign = self._md5(f"nonce={nonce}&source={_SOURCE}POWER_VIEW")
            r = self.session.get(
                f"{BASE_URL}/anonymous/publicKey",
                params={"nonce": nonce, "source": _SOURCE, "sign": sign},
                timeout=HTTP_TIMEOUT)
            if r.status_code >= 500 or r.status_code == 429:
                raise _HTTPStatusError(r.status_code, "publicKey (transient)",
                                       retry_after=_retry_after_seconds(r))
            body = r.json() if r.ok else {}
            raw_key = body.get("data")
            if not (r.ok and body.get("success") and raw_key):
                raise SunsynkAuthError("public key fetch rejected")
            enc_pw = self._rsa_encrypt(raw_key, self.password)
            login_nonce = self._nonce()
            login_sign = self._md5(
                f"nonce={login_nonce}&source={_SOURCE}{raw_key[:10]}")
            payload = {
                "username": self.username, "password": enc_pw,
                "grant_type": "password", "client_id": _CLIENT_ID,
                "source": _SOURCE, "nonce": login_nonce, "sign": login_sign,
            }
            r = self.session.post(f"{BASE_URL}/oauth/token/new", json=payload,
                                  timeout=HTTP_TIMEOUT)
            if r.status_code >= 500 or r.status_code == 429:
                raise _HTTPStatusError(r.status_code, "token/new (transient)",
                                       retry_after=_retry_after_seconds(r))
            body = r.json() if r.ok else {}
            if r.ok and body.get("success") and body.get("data", {}).get("access_token"):
                self._store_token_body(body)
                log.info("Sunsynk login OK (modern flow)")
                return
            raise SunsynkAuthError("modern login rejected")
        except (requests.ConnectionError, requests.Timeout) as e:
            raise SunsynkConnectionError(
                f"network error during login: {type(e).__name__}") from e
        except requests.RequestException as e:
            raise SunsynkError(
                f"bad login response: {type(e).__name__}") from e
        except ValueError as e:   # malformed RSA key material
            raise SunsynkError(f"bad public key: {e}") from e

    def _login_legacy(self) -> None:
        """Plaintext password grant at /oauth/token (older documented flow)."""
        payload = {
            "areaCode": _SOURCE, "client_id": _CLIENT_ID,
            "grant_type": "password", "source": _SOURCE,
            "username": self.username, "password": self.password,
        }
        try:
            r = self.session.post(f"{BASE_URL}/oauth/token", json=payload,
                                  timeout=HTTP_TIMEOUT)
            body = r.json() if r.ok else {}
        except (requests.ConnectionError, requests.Timeout) as e:
            raise SunsynkConnectionError(
                f"network error during login: {type(e).__name__}") from e
        except requests.RequestException as e:
            raise SunsynkError(
                f"bad login response: {type(e).__name__}") from e
        # Spec note: check both code==0 and success (community inconsistency).
        valid = (r.ok and body.get("success")
                 and (body.get("code") in (0, None))
                 and body.get("data", {}).get("access_token"))
        if valid:
            self._store_token_body(body)
            log.info("Sunsynk login OK (legacy flow)")
            return
        raise SunsynkAuthError("legacy login rejected")

    def _relogin_locked(self) -> None:
        """Modern-then-legacy login; caller holds self._login_lock."""
        try:
            self._login_modern()
        except SunsynkAuthError as e_modern:
            log.warning("Modern Sunsynk login rejected (%s); trying legacy "
                        "flow", e_modern)
            try:
                self._login_legacy()
            except SunsynkAuthError as e_legacy:
                raise SunsynkAuthError(
                    f"both auth flows failed: modern={e_modern}; "
                    f"legacy={e_legacy}") from e_legacy

    def login(self, force: bool = False) -> "SunsynkClient":
        """Ensure a valid token. Tries cache, then modern flow, then legacy.
        Lock-guarded with a double-check so concurrent realtime fetches trigger
        at most one actual login."""
        if not self.username or not self.password:
            raise SunsynkAuthError("Sunsynk username/password not configured")
        if not force and (self._token and self._token_expiry > time.time() + 60):
            return self
        with self._login_lock:
            # Re-check under the lock — another thread may have just logged in.
            if not force and (self._token and self._token_expiry > time.time() + 60):
                return self
            if not force and self._load_cached_token():
                return self
            # Falls back to the legacy plaintext flow ONLY on a definitive
            # rejection (SunsynkAuthError). A transient 5xx/429/network failure
            # propagates instead — otherwise routine platform flakiness would
            # re-send the raw password at the application layer.
            self._relogin_locked()
        return self

    # ----- HTTP -----

    @retry(stop=stop_after_attempt(3),
           wait=_wait_honouring_retry_after,
           retry=retry_if_exception(_is_retryable),
           reraise=True)
    def _get(self, path: str, params: dict | None = None) -> dict:
        if not self._token:
            self.login()
        url = f"{BASE_URL}/{path.lstrip('/')}"
        headers = {"Authorization": f"Bearer {self._token}",
                   "Content-Type": "application/json"}
        try:
            r = self.session.get(url, params=params, headers=headers,
                                 timeout=HTTP_TIMEOUT)
            if r.status_code == 401:
                # Token expired/invalid. Serialised on the login lock so the
                # parallel realtime fetches (3 threads hitting 401 together)
                # re-login ONCE: whoever is second sees the token has already
                # moved on and just retries with it.
                failing = headers["Authorization"][len("Bearer "):]
                with self._login_lock:
                    if self._token and self._token != failing:
                        pass   # another thread already refreshed it
                    else:
                        self._token, self._token_expiry = None, 0.0
                        # Another PROCESS (web vs cron) may have refreshed the
                        # cache file — prefer that over a relogin storm.
                        if self._load_cached_token() and self._token != failing:
                            log.info("Sunsynk 401 — using fresher cached token")
                        else:
                            log.info("Sunsynk 401 — clearing token and re-logging in")
                            self._clear_token(if_token=failing)
                            self._relogin_locked()
                headers["Authorization"] = f"Bearer {self._token}"
                r = self.session.get(url, params=params, headers=headers,
                                     timeout=HTTP_TIMEOUT)
            if r.status_code >= 400:
                raise _HTTPStatusError(r.status_code, r.text[:200],
                                       retry_after=_retry_after_seconds(r))
            return r.json()
        except (requests.ConnectionError, requests.Timeout) as e:
            # Wrap at the client boundary: callers catch SunsynkError; a raw
            # requests exception escaping here 500s API routes and aborts the
            # daily sync (observed live 2026-06-06).
            raise SunsynkConnectionError(
                f"network error: {type(e).__name__}") from e
        except requests.RequestException as e:   # incl. JSON decode of a 200
            raise SunsynkError(f"bad response: {type(e).__name__}") from e

    def _get_parallel(self, *paths: str) -> list[dict]:
        """Fetch several endpoints concurrently, preserving order. Concurrent
        callers share one token (login is lock-guarded), so a cold cache still
        logs in only once."""
        with ThreadPoolExecutor(max_workers=len(paths)) as ex:
            return list(ex.map(self._get, paths))

    # ----- discovery / connection test -----

    def get_plants(self) -> list[dict]:
        body = self._get("api/v1/plants",
                         params={"page": 1, "limit": 20, "name": "", "status": ""})
        return (body.get("data") or {}).get("infos", []) or []

    def get_inverters(self) -> list[dict]:
        body = self._get("api/v1/inverters",
                         params={"page": 1, "limit": 20, "status": -1})
        return (body.get("data") or {}).get("infos", []) or []

    def test_connection(self) -> dict:
        """Login and list plants — used by the /setup 'Test connection' button."""
        self.login(force=True)
        plants = self.get_plants()
        return {"ok": True,
                "plants": [{"id": p.get("id"), "name": p.get("name")}
                           for p in plants]}

    # ----- daily data -----

    def fetch_day(self, date: str) -> DayResult:
        """Fetch one local day (YYYY-MM-DD) of energy-flow data.

        Returns a DayResult with half-hourly solar_intervals rows and integrated
        daily kWh totals. On a clean "no data for this day" response, returns
        DayResult(empty=True) so backfill can stop walking backwards.
        """
        if not self.plant_id:
            raise SunsynkError("SUNSYNK_PLANT_ID not configured")
        try:
            body = self._get(_DAY_ENERGY_PATH.format(plant_id=self.plant_id),
                             params={"date": date, "lan": "en"})
        except _HTTPStatusError as e:
            log.warning("Sunsynk /day fetch failed for %s: %s", date, e)
            return DayResult(date=date, ok=False)
        return self._parse_day(date, body)

    def _parse_day(self, date: str, body: dict) -> DayResult:
        infos = self._extract_series_infos(body)
        if not infos:
            return DayResult(date=date, empty=True)

        series = self._classify_series(infos)
        # Labels with no records (a day before the plant's history) is still
        # an empty day — it used to count as data and reset the backfill's
        # end-of-history streak.
        if not any((info or {}).get("records") for info in series.values()):
            return DayResult(date=date, empty=True)

        res = DayResult(date=date)

        def recs(name):
            return series.get(name, {}).get("records", [])

        def unit(name):
            return series.get(name, {}).get("unit", "kW")

        res.pv_kwh = integrate_series_kwh(recs("pv"), unit("pv"), date)
        res.house_load_kwh = integrate_series_kwh(recs("load"), unit("load"), date)

        # Battery: explicit charge/discharge series if present, else split the
        # single signed Battery series (+charge / -discharge).
        if series.get("battery_charge") or series.get("battery_discharge"):
            res.battery_charge_kwh = integrate_series_kwh(
                recs("battery_charge"), unit("battery_charge"), date)
            res.battery_discharge_kwh = integrate_series_kwh(
                recs("battery_discharge"), unit("battery_discharge"), date)
        elif series.get("battery"):
            pos, neg = integrate_signed_kwh(recs("battery"), unit("battery"), date)
            if battery_sign(recs("battery"), recs("soc"), date, unit("battery")) > 0:
                res.battery_charge_kwh, res.battery_discharge_kwh = pos, neg
            else:
                res.battery_charge_kwh, res.battery_discharge_kwh = neg, pos

        # Grid: explicit import/export series if present, else split the single
        # signed Grid series (+import / -export).
        if series.get("grid_import") or series.get("grid_export"):
            res.grid_import_kwh = integrate_series_kwh(
                recs("grid_import"), unit("grid_import"), date)
            res.grid_export_kwh = integrate_series_kwh(
                recs("grid_export"), unit("grid_export"), date)
        elif series.get("grid"):
            res.grid_import_kwh, res.grid_export_kwh = \
                integrate_signed_kwh(recs("grid"), unit("grid"), date)

        res.peak_soc_pct = peak_value(recs("soc"))
        res.end_soc_pct = last_value(recs("soc"))

        res.intervals = self._build_intervals(date, series)
        return res

    @staticmethod
    def _extract_series_infos(body: dict) -> list:
        """Pull the list of series dicts from a /day response, tolerating the
        common nestings: data.infos / data.charts / data (list)."""
        data = body.get("data")
        if isinstance(data, dict):
            for key in ("infos", "charts", "series"):
                if isinstance(data.get(key), list):
                    return data[key]
        if isinstance(data, list):
            return data
        return []

    @staticmethod
    def _classify_series(infos: list) -> dict[str, dict]:
        """Map each series info to a known metric by its label substring."""
        out: dict[str, dict] = {}
        for info in infos:
            if not isinstance(info, dict):
                continue
            label = str(info.get("label") or info.get("name")
                        or info.get("type") or "").lower()
            for metric, needles in _SERIES_MATCHERS.items():
                if metric in out:
                    continue
                if any(n in label for n in needles):
                    out[metric] = info
                    break
        return out

    @staticmethod
    def _build_intervals(date: str, series: dict[str, dict]) -> list[dict]:
        """Produce up to 48 solar_intervals rows from the classified series."""
        def energy_buckets(name):
            info = series.get(name)
            return (resample_halfhourly_energy(info.get("records", []), date,
                                               info.get("unit", "kW"))
                    if info else {})

        def signed_buckets(name):
            info = series.get(name)
            return (resample_halfhourly_signed(info.get("records", []), date,
                                               info.get("unit", "kW"))
                    if info else ({}, {}))

        energy = {"pv_kwh": energy_buckets("pv"),
                  "house_load_kwh": energy_buckets("load")}

        if series.get("battery_charge") or series.get("battery_discharge"):
            energy["battery_charge_kwh"] = energy_buckets("battery_charge")
            energy["battery_discharge_kwh"] = energy_buckets("battery_discharge")
        elif series.get("battery"):
            pos, neg = signed_buckets("battery")
            b = series["battery"]
            plus_is_charge = battery_sign(b.get("records", []),
                                          (series.get("soc") or {}).get("records", []),
                                          date, b.get("unit", "kW")) > 0
            energy["battery_charge_kwh"] = pos if plus_is_charge else neg
            energy["battery_discharge_kwh"] = neg if plus_is_charge else pos

        if series.get("grid_import") or series.get("grid_export"):
            energy["grid_import_kwh"] = energy_buckets("grid_import")
            energy["grid_export_kwh"] = energy_buckets("grid_export")
        elif series.get("grid"):
            imp, exp = signed_buckets("grid")
            energy["grid_import_kwh"], energy["grid_export_kwh"] = imp, exp

        soc = (resample_halfhourly_last(series["soc"].get("records", []), date)
               if series.get("soc") else {})
        keys = set(soc)
        for d in energy.values():
            keys.update(d)
        rows = []
        for k in sorted(keys):
            row = {"interval_start": k, "soc_pct": soc.get(k)}
            for col, data in energy.items():
                row[col] = data.get(k)
            rows.append(row)
        return rows

    # ----- live / realtime -----

    def get_realtime(self) -> dict:
        """Current power snapshot from the verified realtime endpoints.

        Signs match the /day convention: grid + = import / - = export;
        battery + = charge / - = discharge. House load is derived from the
        power balance: load = pv - battery + grid (verified against live data).
        Today's running kWh totals come from the etoday* scalars."""
        sn = self.inverter_sn
        if not sn:
            raise SunsynkError("SUNSYNK_INVERTER_SN not configured")

        def num(d, k, default=None):
            # None, not 0, for a missing reading: an empty input payload
            # showed "0 W solar" and skewed the derived house load.
            try:
                return float(d.get(k))
            except (TypeError, ValueError):
                return default

        inp_b, grid_b, batt_b = self._get_parallel(
            f"api/v1/inverter/{sn}/realtime/input",
            f"api/v1/inverter/grid/{sn}/realtime?sn={sn}",
            f"api/v1/inverter/battery/{sn}/realtime?sn={sn}&lan")
        inp = inp_b.get("data") or {}
        grid = grid_b.get("data") or {}
        batt = batt_b.get("data") or {}

        # The realtime endpoints occasionally return an empty 200 (transient /
        # rate-limit), which would look like an all-zero snapshot (incl. SoC=0).
        # Treat a missing battery SoC as "no reading" rather than emit zeros.
        if batt.get("soc") is None and "pac" not in grid:
            raise SunsynkError("empty realtime response (transient)")

        # The realtime battery endpoint reports power as +discharge / -charge —
        # the OPPOSITE of the /day series. Negate to the documented
        # +charge / -discharge convention so battery_dir and the load balance
        # (load = pv - battery + grid) come out right. Verified live 2026-05-27:
        # SoC 99%, 1.76 kW PV in, raw power -958 = CHARGING at 958 W, true house
        # load 868 W (the old code reported "discharge" and load 2784 W).
        pv_w, grid_w = num(inp, "pac"), num(grid, "pac")
        raw_batt = num(batt, "power")
        batt_w = -raw_batt if raw_batt is not None else None
        strings = [s for s in (inp.get("pvIV") or []) if isinstance(s, dict)]
        known = None not in (pv_w, grid_w, batt_w)
        # Grid supply quality (power_quality.py): voltage, frequency, and
        # whether the inverter sees the grid (status + AC relay; verified live
        # 2026-10-27: {"vip": [{"volt": "248.5"}], "fac": 50.03, "status": 1,
        # "acRealyStatus": 1}).
        vip = grid.get("vip") or []
        grid_v = num(vip[0], "volt") if vip and isinstance(vip[0], dict) else None
        status, relay = grid.get("status"), grid.get("acRealyStatus")
        grid_ok = (None if status is None and relay is None
                   else status != 0 and relay != 0 and (grid_v is None or grid_v >= 100))
        return {
            "grid_v": grid_v,
            "grid_hz": num(grid, "fac"),
            "grid_ok": grid_ok,
            "pv_w": pv_w,
            "grid_w": grid_w,
            "battery_w": batt_w,
            "load_w": round(pv_w - batt_w + grid_w) if known else None,
            # None (not 0.0) when the battery sub-payload is empty — a false
            # "SoC 0%" is alarm-bait for the battery-low alert and the UI.
            "soc_pct": num(batt, "soc", None),
            "battery_count": batt.get("numberOfBatteries"),
            "pv_string_count": len(strings) or None,
            "grid_dir": (None if grid_w is None
                         else "import" if grid_w >= 0 else "export"),
            "battery_dir": (None if batt_w is None else "charge" if batt_w > 0
                            else "discharge" if batt_w < 0 else "idle"),
            "today": {
                "pv_kwh": num(inp, "etoday"),
                "import_kwh": num(grid, "etodayFrom"),
                "export_kwh": num(grid, "etodayTo"),
                "charge_kwh": num(batt, "etodayChg"),
                "discharge_kwh": num(batt, "etodayDischg"),
            },
        }

    def get_indepth(self) -> dict:
        """Detailed battery + PV-string stats for the In-Depth view. Sunsynk's
        realtime payload is sometimes sparse, so every field is optional and
        returned as None when absent (the UI shows '–')."""
        sn = self.inverter_sn
        if not sn:
            raise SunsynkError("SUNSYNK_INVERTER_SN not configured")

        def num(d, k):
            v = d.get(k)
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        # The realtime battery endpoint reports power/current as +discharge /
        # -charge. Flip battery power + current to the app-wide +charge /
        # -discharge convention (see get_realtime) so In-Depth agrees with the
        # dashboard flow. PV-string values are left raw (generation is +ve).
        def neg(d, k):
            v = num(d, k)
            return -v if v is not None else None

        batt_b, inp_b = self._get_parallel(
            f"api/v1/inverter/battery/{sn}/realtime?sn={sn}&lan",
            f"api/v1/inverter/{sn}/realtime/input")
        batt = batt_b.get("data") or {}
        inp = inp_b.get("data") or {}

        # Per-battery modules: only when the BMS actually reports them (a live
        # voltage > 0). Many inverters report only the aggregate, leaving the
        # batterySoc{i}/Volt{i} fields at 0/None — don't show phantom modules.
        modules = []
        for i in (1, 2):
            if (num(batt, f"batteryVolt{i}") or 0) > 0:
                modules.append({
                    "n": i, "soc": num(batt, f"batterySoc{i}"),
                    "voltage": num(batt, f"batteryVolt{i}"),
                    "current": neg(batt, f"batteryCurrent{i}"),
                    "power": neg(batt, f"batteryPower{i}"),
                    "temp": num(batt, f"batteryTemp{i}"),
                })

        # PV strings: Sunsynk uses pvNo / vpv / ipv / ppv / todayPv per string.
        strings = []
        for s in (inp.get("pvIV") or inp.get("mpptIV") or []):
            if isinstance(s, dict):
                strings.append({
                    "n": s.get("pvNo"),
                    "voltage": num(s, "vpv"),
                    "current": num(s, "ipv"),
                    "power": num(s, "ppv"),
                    "today_kwh": num(s, "todayPv"),
                })

        # Throughput-based state-of-health ESTIMATE (not a measurement): the
        # canonical EoL is ~80% nameplate, so model 20pp fade over rated cycles.
        import config as _cfg
        nameplate = _cfg.get_number("battery_nameplate_kwh")
        rated_cycles = _cfg.get_number("battery_rated_cycles", 6000)
        total_dis = num(batt, "etotalDischg")
        cycles = soh = None
        if nameplate and total_dis:
            cycles = round(total_dis / nameplate, 1)
            if rated_cycles:
                soh = round(max(0.0, 100 - (cycles / rated_cycles) * 20), 1)

        return {
            "battery": {
                "nameplate_kwh": nameplate,
                "cycle_equivalents": cycles,
                "soh_pct": soh,
                "soc": num(batt, "soc"), "voltage": num(batt, "voltage"),
                "current": neg(batt, "current"), "power": neg(batt, "power"),
                "temp": num(batt, "temp"),
                "bms_soc": num(batt, "bmsSoc"), "bms_voltage": num(batt, "bmsVolt"),
                "bms_current": neg(batt, "bmsCurrent"), "bms_temp": num(batt, "bmsTemp"),
                "capacity_ah": num(batt, "capacity"),
                "corrected_capacity_ah": num(batt, "correctCap"),
                "charge_volt": num(batt, "chargeVolt"),
                "discharge_volt": num(batt, "dischargeVolt"),
                "charge_current_limit": num(batt, "chargeCurrentLimit"),
                "discharge_current_limit": num(batt, "dischargeCurrentLimit"),
                "number_of_batteries": batt.get("numberOfBatteries"),
                "today_charge_kwh": num(batt, "etodayChg"),
                "today_discharge_kwh": num(batt, "etodayDischg"),
                "month_charge_kwh": num(batt, "emonthChg"),
                "month_discharge_kwh": num(batt, "emonthDischg"),
                "year_charge_kwh": num(batt, "eyearChg"),
                "year_discharge_kwh": num(batt, "eyearDischg"),
                "total_charge_kwh": num(batt, "etotalChg"),
                "total_discharge_kwh": num(batt, "etotalDischg"),
                "bms_version": batt.get("bms1Version1"),
                "modules": modules,
            },
            "pv_today_kwh": num(inp, "etoday"),
            "pv_total_kwh": num(inp, "etotal"),
            "pv_now_w": num(inp, "pac"),
            "pv_strings": strings,
        }

    def close(self) -> None:
        self.session.close()
