"""Per-consumer tokens for the read-only home API (/api/home/*, /metrics).

One shared token made a misconfigured consumer invisible: a heating controller sent a
wrong token ~1,000 times a day for weeks, and the only sign was a WARNING
line per request. Each consumer now has its own token (stored as a SHA-256
hash — tokens are 256-bit random, so the hash is safe to keep) with
last-seen / IP / request counters, and rejected tokens are tracked by a short
fingerprint (never the token) with one log line per fingerprint per hour and
a once-a-day Mattermost alert. The legacy shared token keeps working and is
listed as its own consumer until revoked.

Counters are accumulated in memory and written at most once a minute: the
home API is polled every minute by several consumers.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
from datetime import datetime, timezone

import database as db

log = logging.getLogger("powersoc.auth")

ALERT_AFTER = 20            # rejected requests in a day before the alert
_FLUSH_S = 60.0
_LOG_EVERY_S = 3600.0
_REJECTS_KEY = "home_token_rejects"
_LEGACY_STATS_KEY = "home_legacy_stats"
LEGACY_ID = "legacy"

_lock = threading.Lock()
_pending: dict = {}          # consumer id -> {"n", "ip", "at"}
_rejects: dict = {}          # fingerprint -> live counters since last flush
_last_flush = 0.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _today() -> str:
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("Europe/London")).date().isoformat()


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8", "surrogateescape")).hexdigest()


def fingerprint(token: str) -> str:
    """8 hex chars identifying a token in the UI/logs without revealing it."""
    return _hash(token)[:8]


# ---------- consumers ---------------------------------------------------------

def create(name: str) -> dict:
    """Mint a token for a named consumer. The token is returned ONCE."""
    token = secrets.token_urlsafe(32)
    with db.conn() as c:
        cur = c.execute("INSERT INTO home_consumers(name, token_hash) VALUES(?,?)",
                        (name, _hash(token)))
    return {"id": cur.lastrowid, "name": name, "token": token,
            "fingerprint": fingerprint(token)}


def revoke(consumer_id) -> None:
    if consumer_id == LEGACY_ID:
        import auth
        auth.home_api_token_set(None)
        db.setting_set(_LEGACY_STATS_KEY, None)
        return
    with db.conn() as c:
        c.execute("DELETE FROM home_consumers WHERE id=?", (int(consumer_id),))
    with _lock:
        _pending.pop(int(consumer_id), None)


def any_configured() -> bool:
    import auth
    if auth.home_api_enabled():
        return True
    with db.conn() as c:
        return c.execute("SELECT 1 FROM home_consumers LIMIT 1").fetchone() is not None


def identify(presented: str | None) -> dict | None:
    """The consumer a presented token belongs to, or None."""
    if not presented:
        return None
    with db.conn() as c:
        row = c.execute("SELECT id, name FROM home_consumers WHERE token_hash=?",
                        (_hash(presented),)).fetchone()
    if row:
        return {"id": row["id"], "name": row["name"]}
    import auth
    legacy = auth.home_api_token_get()
    # Compare as bytes: compare_digest raises TypeError on non-ASCII str.
    if legacy and hmac.compare_digest(presented.encode("utf-8", "surrogateescape"),
                                      legacy.encode("utf-8", "surrogateescape")):
        return {"id": LEGACY_ID, "name": "Shared token (legacy)"}
    return None


def list_consumers() -> list[dict]:
    flush()
    with db.conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT id, name, created_at, last_seen_at, last_ip, requests "
            "FROM home_consumers ORDER BY id")]
    import auth
    if auth.home_api_enabled():
        stats = _load(_LEGACY_STATS_KEY)
        rows.insert(0, {"id": LEGACY_ID, "name": "Shared token (legacy)",
                        "created_at": None, "last_seen_at": stats.get("last_seen_at"),
                        "last_ip": stats.get("last_ip"),
                        "requests": stats.get("requests", 0)})
    return rows


# ---------- accounting --------------------------------------------------------

def record_use(consumer: dict, ip: str | None) -> None:
    with _lock:
        p = _pending.setdefault(consumer["id"], {"n": 0})
        p.update(n=p["n"] + 1, ip=ip, at=_now_iso())
    _maybe_flush()


def record_reject(presented: str | None, ip: str | None, path: str) -> None:
    """Count a rejected request; log the first one per fingerprint, then a
    summary at most hourly (the old per-request WARNING flooded the log)."""
    fp = fingerprint(presented) if presented else "(none)"
    now = time.time()
    with _lock:
        r = _rejects.setdefault(fp, {"n": 0, "logged_at": 0.0, "since_log": 0})
        r.update(n=r["n"] + 1, ip=ip, path=path, at=_now_iso())
        r["since_log"] += 1
        first = r["logged_at"] == 0.0 and not _load(_REJECTS_KEY).get(fp)
        due = now - r["logged_at"] >= _LOG_EVERY_S
        if first or due:
            n, r["since_log"], r["logged_at"] = r["since_log"], 0, now
        else:
            n = 0
    if n and first:
        log.warning("Rejected a home-API request with an unknown token (…%s) from %s "
                    "for %r — further rejections are summarised hourly", fp, ip, path)
    elif n:
        log.warning("Rejected %d home-API request(s) with unknown token …%s from %s "
                    "in the last hour", n, fp, ip)
    _maybe_flush()


def _load(key: str) -> dict:
    try:
        return json.loads(db.setting_get(key) or "{}")
    except (ValueError, TypeError):
        return {}


def _maybe_flush() -> None:
    if time.time() - _last_flush >= _FLUSH_S:
        flush()


def flush() -> None:
    """Write accumulated counters (best-effort; never raises)."""
    global _last_flush
    with _lock:
        pending = dict(_pending)
        _pending.clear()
        rejects = {fp: dict(r) for fp, r in _rejects.items() if r["n"]}
        for r in _rejects.values():
            r["n"] = 0
        _last_flush = time.time()
    try:
        legacy = pending.pop(LEGACY_ID, None)
        if pending:
            with db.conn() as c:
                c.executemany(
                    "UPDATE home_consumers SET requests=requests+?, last_seen_at=?, "
                    "last_ip=? WHERE id=?",
                    [(p["n"], p["at"], p["ip"], cid) for cid, p in pending.items()])
        if legacy:
            st = _load(_LEGACY_STATS_KEY)
            st.update(requests=st.get("requests", 0) + legacy["n"],
                      last_seen_at=legacy["at"], last_ip=legacy["ip"])
            db.setting_set(_LEGACY_STATS_KEY, json.dumps(st))
        if rejects:
            stored = _load(_REJECTS_KEY)
            today = _today()
            for fp, r in rejects.items():
                s = stored.get(fp) or {"first_at": r["at"], "total": 0}
                if s.get("day") != today:
                    s.update(day=today, count_today=0)
                s.update(total=s.get("total", 0) + r["n"],
                         count_today=s.get("count_today", 0) + r["n"],
                         last_at=r["at"], last_ip=r["ip"], last_path=r["path"])
                stored[fp] = s
            # Keep the 20 most recent fingerprints.
            keep = sorted(stored.items(), key=lambda kv: kv[1].get("last_at") or "")[-20:]
            db.setting_set(_REJECTS_KEY, json.dumps(dict(keep)))
    except Exception:  # noqa: BLE001 — accounting must never break a request
        log.exception("Home-API usage flush failed")


def rejections() -> list[dict]:
    """Rejected-token fingerprints, most recent first (count_today resets daily)."""
    flush()
    today = _today()
    out = []
    for fp, s in _load(_REJECTS_KEY).items():
        out.append({"fingerprint": fp, "total": s.get("total", 0),
                    "count_today": s.get("count_today", 0) if s.get("day") == today else 0,
                    "first_at": s.get("first_at"), "last_at": s.get("last_at"),
                    "last_ip": s.get("last_ip"), "last_path": s.get("last_path")})
    return sorted(out, key=lambda r: r["last_at"] or "", reverse=True)


def alert_candidates() -> list[tuple[str, str]]:
    """(key, message) for alerts.check_alerts — once a day per instance."""
    bad = [r for r in rejections() if r["count_today"] >= ALERT_AFTER]
    if not bad:
        return []
    r = bad[0]
    more = f" (+{len(bad) - 1} more)" if len(bad) > 1 else ""
    return [("home_token_rejects",
             f"🔑 A home-API consumer keeps using an invalid home-API token: …{r['fingerprint']} "
             f"from **{r['last_ip']}** ({r['count_today']} rejected requests today, last "
             f"{r['last_path']}){more}. Give it its own token in PowerSOC Settings → Home API.")]


def reset_state() -> None:
    """Tests: drop in-memory counters."""
    global _last_flush
    with _lock:
        _pending.clear()
        _rejects.clear()
        _last_flush = 0.0
