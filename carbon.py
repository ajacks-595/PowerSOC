"""UK grid carbon intensity via NESO's Carbon Intensity API (free, no key).

Uses the regional postcode endpoint so the right DNO region resolves itself
(Dumfries DG1 -> South Scotland) without hard-coding a region number. The
endpoint wants a postcode DISTRICT (outward code, "DG1"): a bare area ("DG")
gets HTTP 200 with a `null` body. Cached; fails soft (and caches the failure,
so an outage isn't re-requested on every page load). CC-BY 4.0 data from
https://carbonintensity.org.uk.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone

import requests

log = logging.getLogger("powersoc.carbon")

_DEFAULT_OUTCODE = "DG1"   # Dumfries
_TTL = 1800       # 30 min
_FAIL_TTL = 600   # back off this long after a failed fetch
_cache: dict = {"ts": 0.0, "data": None, "fail_until": 0.0}
_lock = threading.Lock()

# A UK outward code: area (1-2 letters) + district (digit, optional char).
OUTCODE_RE = re.compile(r"^[A-Z]{1,2}[0-9][0-9A-Z]?$")


def normalise_outcode(raw: str) -> str:
    """'dg1 2ab' / ' DG1 ' -> 'DG1': the outward half of a postcode, upper
    case, alphanumerics only (it is interpolated into the request path)."""
    first = (raw or "").strip().upper().split()
    return "".join(ch for ch in (first[0] if first else "")
                   if ch.isascii() and ch.isalnum())[:4]


def _outcode() -> str:
    """The configured district. The old default was the bare area "DG", which
    the API answers with `null` — the panel never rendered (2026-10). An area
    alone (env var or older setting) falls back to its "1" district, which is
    in the same DNO region in practice."""
    import config
    code = normalise_outcode(config.get_text(
        "carbon_outcode",
        os.environ.get("POWERSOC_CARBON_OUTCODE", _DEFAULT_OUTCODE))
        or _DEFAULT_OUTCODE) or _DEFAULT_OUTCODE
    if not any(ch.isdigit() for ch in code):
        code = code[:2] + "1"
    return code


def _slot_from(s: dict) -> datetime | None:
    try:
        return datetime.strptime(s.get("from") or "", "%Y-%m-%dT%H:%MZ").replace(
            tzinfo=timezone.utc)
    except ValueError:
        return None


def _unavailable() -> dict:
    """Serve the cached 24h forecast during an outage — re-read for NOW.
    It used to return the cached "current" slot unchanged (hours old) with
    no age limit; now the current/greenest slots are the ones still ahead,
    and once every slot has passed there is nothing honest to show."""
    data = _cache["data"]
    if not data:
        return {"available": False, "region": None}
    now = datetime.now(timezone.utc)
    ahead = [s for s in data.get("slots") or []
             if (f := _slot_from(s)) is not None and f + timedelta(minutes=30) > now
             and s.get("g") is not None]
    if not ahead:
        return {"available": False, "region": data.get("region")}
    cur, green = ahead[0], min(ahead, key=lambda s: s["g"])
    win = data.get("greenest_window_3h")
    if win and (win.get("from") or "") < cur["from"]:
        win = None
    return {**data, "stale": True, "slots": ahead,
            "current": {"g": cur["g"], "index": cur.get("index")},
            "greenest": {"from": green["from"], "g": green["g"],
                         "index": green.get("index")},
            "greenest_window_3h": win}


def _g(slot: dict):
    return (slot.get("intensity") or {}).get("forecast")


def _contiguous(win: list[dict]) -> bool:
    """Each slot's `from` equals the previous slot's `to` (no gap from a
    filtered-out null-forecast slot, which would stretch '3h' to 3.5h+)."""
    return all(a.get("to") and a.get("to") == b.get("from")
               for a, b in zip(win, win[1:]))


def _greenest_window_3h(slots: list[dict]) -> dict | None:
    """Lowest-average rolling 3h (6 CONTIGUOUS half-hour slots) window — more
    actionable for charging/appliances than a single greenest half-hour."""
    best = None
    for i in range(len(slots) - 5):
        win = slots[i:i + 6]
        vals = [_g(s) for s in win]
        if any(v is None for v in vals) or not _contiguous(win):
            continue
        avg = sum(vals) / 6.0
        if best is None or avg < best[0]:
            best = (avg, win[0].get("from"), win[5].get("to"))
    if best is None:
        return None
    return {"from": best[1], "to": best[2], "avg_g": round(best[0])}


def get_intensity() -> dict:
    """Current regional intensity + index, the greenest upcoming half-hour
    (plus the greenest rolling 3h window), and the 24h forecast slots."""
    now = time.time()
    if _cache["data"] and now - _cache["ts"] < _TTL:
        return _cache["data"]
    if now < _cache["fail_until"]:
        return _unavailable()
    with _lock:
        if _cache["data"] and time.time() - _cache["ts"] < _TTL:
            return _cache["data"]
        if time.time() < _cache["fail_until"]:
            return _unavailable()
        try:
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
            code = _outcode()
            r = requests.get(
                f"https://api.carbonintensity.org.uk/regional/intensity/{ts}/fw24h/postcode/{code}",
                headers={"Accept": "application/json", "User-Agent": "PowerSOC/1.0"},
                timeout=10)
            r.raise_for_status()
            body = r.json()
            if not body:
                # 200 + `null`: the API doesn't recognise the district.
                raise ValueError(f"no data for postcode district {code!r}")
            region = body.get("data", {})
            # Some NESO endpoints wrap the region in a list — normalise.
            if isinstance(region, list):
                region = region[0] if region else {}
            slots = [s for s in region.get("data", [])
                     if _g(s) is not None]
            if not slots:
                raise ValueError("no slots")
            cur = slots[0]
            greenest = min(slots, key=_g)
            data = {
                "available": True,
                "region": region.get("shortname"),
                "current": {"g": _g(cur), "index": (cur.get("intensity") or {}).get("index")},
                "greenest": {"from": greenest.get("from"), "to": greenest.get("to"),
                             "g": _g(greenest),
                             "index": (greenest.get("intensity") or {}).get("index")},
                "greenest_window_3h": _greenest_window_3h(slots),
                "slots": [{"from": s.get("from"), "g": _g(s),
                           "index": (s.get("intensity") or {}).get("index")} for s in slots],
            }
            _cache.update(ts=now, data=data, fail_until=0.0)
            return data
        except (requests.RequestException, ValueError, KeyError,
                TypeError, AttributeError) as e:
            log.warning("Carbon intensity fetch failed: %s", e)
            _cache["fail_until"] = time.time() + _FAIL_TTL
            return _unavailable()
