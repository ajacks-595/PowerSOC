"""Shared web helpers: the {success,data,error} JSON envelope, request-body
validation, and the per-key TTL cache shared by every upstream-backed route."""
from __future__ import annotations

import logging
import threading
import time

from flask import jsonify, request

log = logging.getLogger("powersoc.common")


# ---------- JSON API envelope ---------------------------------------------

def ok(data=None, **extra):
    return jsonify({"success": True, "data": data, "error": None, **extra})


# Short-TTL cache so concurrent requests / multiple tabs / the home API all
# share ONE upstream (Sunsynk) fetch instead of hammering it — this both
# improves performance and avoids rate-limiting that was returning sparse
# realtime payloads (missing battery detail).
_snap_cache: dict = {}
_snap_locks: dict = {}
_snap_meta_lock = threading.Lock()
_FAIL_TTL = 15.0   # how long a failure suppresses re-fetch attempts


class _CachedFailure:
    """Negative-cache marker: re-raise the original error for _FAIL_TTL."""
    def __init__(self, exc: Exception):
        self.exc = exc


def _key_lock(key: str) -> threading.Lock:
    with _snap_meta_lock:
        return _snap_locks.setdefault(key, threading.Lock())


def _cache_clear() -> None:
    with _snap_meta_lock:
        _snap_cache.clear()


def _cached(key: str, ttl: float, fn):
    """TTL cache with PER-KEY locks (one slow Sunsynk fetch must not block
    unrelated keys — a single global lock could starve all worker threads
    during an outage), stale-while-error (a refresh failure serves the last
    good value marked `stale: true` instead of erroring, riding out the
    documented Sunsynk 502 storms), and short negative caching (a failure is
    not retried for _FAIL_TTL, so polls don't stack 60s retry chains)."""
    def _unwrap(entry):
        if isinstance(entry[1], _CachedFailure):
            raise entry[1].exc
        return entry[1]

    now = time.time()
    ent = _snap_cache.get(key)
    if ent and now - ent[0] < ttl:
        return _unwrap(ent)
    with _key_lock(key):
        ent = _snap_cache.get(key)
        now = time.time()
        if ent and now - ent[0] < ttl:
            return _unwrap(ent)
        try:
            val = fn()
        except Exception as e:
            backdated = now - ttl + _FAIL_TTL   # expires after _FAIL_TTL
            if ent is not None and not isinstance(ent[1], _CachedFailure):
                stale = ent[1]
                if isinstance(stale, dict) and not stale.get("stale"):
                    stale = {**stale, "stale": True}
                _snap_cache[key] = (backdated, stale)
                return stale
            _snap_cache[key] = (backdated, _CachedFailure(e))
            raise
        _snap_cache[key] = (now, val)
        return val


def err(message: str, code: int = 400, **extra):
    return jsonify({"success": False, "data": None, "error": message, **extra}), code


class ApiInputError(Exception):
    """Malformed client input → 400 JSON envelope (see create_app)."""


def _json_body() -> dict:
    """The request's JSON body as a dict ({} when absent). A JSON array /
    string / number used to reach `body.items()` / `body.get()` and 500."""
    body = request.get_json(silent=True)
    if body is None:
        return {}
    if not isinstance(body, dict):
        raise ApiInputError("JSON body must be an object")
    return body


def _body_str(body: dict, key: str) -> str | None:
    """Optional string field (None when absent/empty); non-strings → 400."""
    v = body.get(key)
    if v is None or v == "":
        return None
    if not isinstance(v, str):
        raise ApiInputError(f"{key} must be a string")
    return v


def _check_webhook(url: str | None) -> None:
    import notifications
    if url and not notifications.valid_webhook_url(url):
        raise ApiInputError("webhook URL must be an http(s) URL")
