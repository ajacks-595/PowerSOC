"""Grid outage and power-quality log (feature 12).

Fed from the live Sunsynk snapshot (polled every minute by the dashboard and
the home-API consumers): one sample a minute of grid voltage, frequency and
whether the inverter sees the grid, kept 30 days. Outages and excursions
outside the UK statutory supply limits (230 V −6%/+10%: 216.2–253 V; 50 Hz
±1%: 49.5–50.5 Hz) become events (kept a year); an outage is announced on
Mattermost when it starts and when the grid comes back."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import database as db

log = logging.getLogger("powersoc.power_quality")
V_LOW, V_HIGH = 216.2, 253.0
HZ_LOW, HZ_HIGH = 49.5, 50.5
KINDS = ("outage", "overvoltage", "undervoltage", "frequency")


def _notify(text: str) -> None:
    """Best-effort Mattermost note (background: never delays the live view)."""
    import threading

    import config
    if (not config.Settings.load().mattermost_configured
            or not config.alert_config()["enabled"] or config.instance_role() == "standby"):
        return

    def send():
        try:
            import notifications
            notifications.post_brief("### ⚡ PowerSOC grid\n" + text)
        except Exception:  # noqa: BLE001 — best-effort
            log.exception("Grid notification failed")
    threading.Thread(target=send, name="powersoc-grid-note", daemon=True).start()


def _present(v, hz, ok) -> set[str]:
    if ok is False:
        return {"outage"}
    out = set()
    if v is not None and v > V_HIGH:
        out.add("overvoltage")
    if v is not None and 0 < v < V_LOW:
        out.add("undervoltage")
    if hz is not None and (hz < HZ_LOW or hz > HZ_HIGH):
        out.add("frequency")
    return out


def record(rt: dict | None, now: datetime | None = None) -> None:
    """Record one live snapshot (at most one sample per minute)."""
    if not rt:
        return
    v, hz, ok = rt.get("grid_v"), rt.get("grid_hz"), rt.get("grid_ok")
    if v is None and hz is None and ok is None:
        return
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    minute = now.replace(second=0, microsecond=0).isoformat()
    with db.conn() as c:
        cur = c.execute("INSERT OR IGNORE INTO grid_samples(ts, volt, hz, ok) VALUES(?,?,?,?)",
                        (minute, v, hz, None if ok is None else int(bool(ok))))
        if cur.rowcount != 1:
            return
        present = _present(v, hz, ok)
        for kind in KINDS:
            row = c.execute("SELECT * FROM grid_events WHERE kind=? AND end IS NULL "
                            "ORDER BY id DESC LIMIT 1", (kind,)).fetchone()
            if kind in present and row is None:
                c.execute("INSERT INTO grid_events(kind, start, min_v, max_v, min_hz, max_hz) "
                          "VALUES(?,?,?,?,?,?)", (kind, minute, v, v, hz, hz))
                if kind == "outage":
                    _notify(f"🔌 **Grid outage** detected at {_local(minute)} — "
                            "the house is on battery/solar.")
            elif kind in present:
                c.execute("UPDATE grid_events SET min_v=min(COALESCE(min_v, ?), ?), "
                          "max_v=max(COALESCE(max_v, ?), ?), min_hz=min(COALESCE(min_hz, ?), ?), "
                          "max_hz=max(COALESCE(max_hz, ?), ?) WHERE id=?",
                          (v, v, v, v, hz, hz, hz, hz, row["id"]))
            elif row is not None:
                c.execute("UPDATE grid_events SET end=? WHERE id=?", (minute, row["id"]))
                if kind == "outage":
                    mins = _minutes(row["start"], minute)
                    _notify(f"✅ **Grid back** at {_local(minute)} after {mins:.0f} min.")


def _local(iso: str) -> str:
    from zoneinfo import ZoneInfo
    return datetime.fromisoformat(iso).astimezone(ZoneInfo("Europe/London")).strftime("%H:%M")


def _minutes(a: str, b: str) -> float:
    return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() / 60


def events(days: int = 30, now: datetime | None = None) -> list[dict]:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    since = (now - timedelta(days=days)).isoformat()
    with db.conn() as c:
        rows = c.execute("SELECT * FROM grid_events WHERE start>=? OR end IS NULL "
                         "ORDER BY start DESC", (since,)).fetchall()
    return [{**dict(r), "ongoing": r["end"] is None,
             "minutes": round(_minutes(r["start"], r["end"] or now.isoformat()), 1)}
            for r in rows]


def summary(days: int = 7, now: datetime | None = None) -> dict:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    since = (now - timedelta(days=days)).isoformat()
    with db.conn() as c:
        r = c.execute("SELECT COUNT(*) n, MIN(volt) min_v, MAX(volt) max_v, AVG(volt) avg_v, "
                      "MIN(hz) min_hz, MAX(hz) max_hz FROM grid_samples WHERE ts>=? "
                      "AND (ok IS NULL OR ok=1)", (since,)).fetchone()
        total = c.execute("SELECT COUNT(*) FROM grid_samples WHERE ts>=?", (since,)).fetchone()[0]
    ev = events(days, now)
    return {"samples": total, "min_v": r["min_v"], "max_v": r["max_v"],
            "avg_v": round(r["avg_v"], 1) if r["avg_v"] else None,
            "min_hz": r["min_hz"], "max_hz": r["max_hz"],
            "outages": sum(1 for e in ev if e["kind"] == "outage"),
            "outage_minutes": round(sum(e["minutes"] for e in ev if e["kind"] == "outage"), 1)}


def series(days: int = 7) -> list[dict]:
    """Half-hourly min/max voltage and frequency for the chart."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with db.conn() as c:
        rows = c.execute(
            "SELECT substr(ts, 1, 14) || CASE WHEN CAST(substr(ts, 15, 2) AS INTEGER) < 30 "
            "THEN '00' ELSE '30' END AS slot, MIN(volt) min_v, MAX(volt) max_v, "
            "MIN(hz) min_hz, MAX(hz) max_hz, MIN(ok) ok FROM grid_samples WHERE ts>=? "
            "GROUP BY slot ORDER BY slot", (since,)).fetchall()
    return [dict(r) for r in rows]


def prune(now: datetime | None = None, keep_days: int = 30) -> int:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    with db.conn() as c:
        cur = c.execute("DELETE FROM grid_samples WHERE ts<?",
                        ((now - timedelta(days=keep_days)).isoformat(),))
        c.execute("DELETE FROM grid_events WHERE end IS NOT NULL AND end<?",
                  ((now - timedelta(days=365)).isoformat(),))
        return cur.rowcount or 0
