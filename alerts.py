"""Intraday Mattermost alerts — fired from the hourly sync, deduped once per day.

These complement the morning brief: they surface conditions *during* the day
(battery low, high spend, heavy peak import, inverter offline). Each condition
fires at most once per day; the fired set is stored in the settings table.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import config
import database as db
import notifications

log = logging.getLogger("powersoc.alerts")
LONDON = ZoneInfo("Europe/London")


def _fired(date: str) -> set[str]:
    raw = db.setting_get("alerts_fired:" + date)
    try:
        return set(json.loads(raw)) if raw else set()
    except (ValueError, TypeError):
        return set()


def _mark(date: str, fired: set[str]) -> None:
    db.setting_set("alerts_fired:" + date, json.dumps(sorted(fired)))


def _prune_old(keep_days: int = 7, keep_date: str = "") -> None:
    """Drop alerts_fired:<date> rows older than keep_days so the settings table
    doesn't accumulate one row per day forever. `keep_date` (the day currently
    being processed) is never pruned — deleting it mid-run would let its alerts
    re-fire on the next call even though dedup was read before pruning."""
    cutoff = (datetime.now(LONDON).date() - timedelta(days=keep_days)).isoformat()
    for key in db.setting_keys_with_prefix("alerts_fired:"):
        d = key.split(":", 1)[1]
        if d < cutoff and d != keep_date:
            db.setting_delete(key)


def _in_quiet_hours(now: datetime) -> bool:
    """Whether `now` falls inside the configured quiet window ("22-7" style,
    local hours, end-exclusive; empty/invalid = no quiet hours)."""
    spec = config.alert_config()["quiet_hours"] or ""
    try:
        start_s, end_s = spec.split("-")
        start, end = int(start_s), int(end_s)
    except (ValueError, AttributeError):
        return False
    h = now.hour
    if start == end:
        return False
    if start < end:
        return start <= h < end
    return h >= start or h < end     # wraps midnight, e.g. 22-7


# Recovery notifications: when a previously-fired condition clears, say so once
# (key, condition-cleared test, message) — deduped through the same fired set.
def _recoveries(summary: dict, fired: set[str]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    soc = summary.get("end_soc_pct")
    low = config.alert_config()["battery_low_pct"]
    if ("battery_low" in fired and "battery_recovered" not in fired
            and soc is not None
            and soc >= low + 10):   # hysteresis
        out.append(("battery_recovered",
                    f"🔋 Battery recovered: **{round(soc)}%**"))
    if ("sunsynk_offline" in fired and "sunsynk_back" not in fired
            and summary.get("sunsynk_ok")):
        out.append(("sunsynk_back", "✅ Sunsynk data flowing again"))
    return out


# Sunsynk uploads every ~5 min; no new record for this long while the day
# goes on means the inverter (or its dongle) stopped reporting.
_OFFLINE_AFTER = timedelta(hours=3)


def evaluate(summary: dict, settings, hour: int,
             last_solar_at: datetime | None = None,
             now: datetime | None = None) -> list[tuple[str, str]]:
    """Pure: which (key, message) alerts apply to this summary right now.

    `hour` is the local hour (used so 'offline' only fires later in the day);
    `last_solar_at` is today's newest Sunsynk record (local, naive)."""
    out: list[tuple[str, str]] = []
    cfg = config.alert_config()
    soc = summary.get("end_soc_pct")
    if soc is not None and soc < cfg["battery_low_pct"]:
        out.append(("battery_low",
                    f"🪫 Battery low: **{round(soc)}%** (below {round(cfg['battery_low_pct'])}%)"))
    net = summary.get("net_cost_pence")
    if net is not None and net > cfg["high_cost_pence"]:
        out.append(("high_cost", f"💸 High spend today: **£{net/100:.2f}** so far"))
    peak = summary.get("peak_import_kwh")
    if peak and peak > cfg["peak_kwh"]:
        out.append(("high_peak",
                    f"⚡ Heavy peak-window import: **{peak:.1f} kWh** — check your schedule"))
    if getattr(settings, "sunsynk_configured", False) and hour >= 12:
        if not summary.get("sunsynk_ok"):
            out.append(("sunsynk_offline",
                        "⚠️ No Sunsynk data today — the inverter may be offline"))
        elif last_solar_at is not None:
            # Data that STOPPED mid-day never alerted: today had rows, so
            # sunsynk_ok stayed 1.
            now = now or last_solar_at.replace(hour=hour, minute=59)
            if now - last_solar_at > _OFFLINE_AFTER:
                out.append(("sunsynk_offline",
                            f"⚠️ No Sunsynk data since {last_solar_at:%H:%M} — "
                            "the inverter may be offline"))
    return out


def _degraded_streak(date: str, settings, days: int = 3) -> list[tuple[str, str]]:
    """A single ⚠️ Sunsynk-degraded day is normal (gotcha #4); `days`
    consecutive dead days means the token/inverter is genuinely broken and
    deserves its own alert rather than hoping the user notices the daily ⚠️."""
    if not getattr(settings, "sunsynk_configured", False):
        return []
    try:
        end = datetime.fromisoformat(date).date()
    except ValueError:
        return []
    start = (end - timedelta(days=days - 1)).isoformat()
    rows = db.daily_summaries_range(start, date)
    if len(rows) >= days and all(not r["sunsynk_ok"] for r in rows):
        return [("sunsynk_streak",
                 f"🚨 No Sunsynk data for {days} consecutive days — "
                 f"check the inverter / Sunsynk Connect login")]
    return []


def check_alerts(date: str) -> list[str]:
    """Evaluate + send any new alerts for `date`. Returns the keys fired now."""
    if not config.alert_config()["enabled"]:
        return []
    settings = config.Settings.load()
    if not settings.mattermost_configured:
        return []
    if config.instance_role() == "standby":
        # The primary sends alerts; a standby covers while it's down — or
        # while its webhook isn't delivering (its brief is overdue).
        import sync
        peer, _why = sync._peer_health()
        if not config.peer_url() or (peer is not None and peer.get("brief_ok") is not False):
            return []
    summary = db.get_daily_summary(date)
    if not summary:
        return []

    # Read the day's dedup record BEFORE pruning. _prune_old drops records older
    # than keep_days, so pruning first would wipe the record for any day already
    # older than the window (e.g. a re-run historical day) and let alerts
    # re-fire. Reading first keeps dedup correct regardless of the date's age,
    # and keep_date stops the prune deleting the record we're about to rewrite.
    now = datetime.now(LONDON)
    fired = _fired(date)
    _prune_old(keep_date=date)
    if _in_quiet_hours(now):
        # Don't send (and don't mark) during quiet hours — a still-true
        # condition fires on the first tick after the window ends.
        return []
    rows = db.intervals_for_date("solar_intervals", date)
    last_solar = None
    if rows:
        try:
            last_solar = datetime.fromisoformat(rows[-1]["interval_start"]).replace(
                tzinfo=None) + timedelta(minutes=30)        # end of that half-hour
        except ValueError:
            last_solar = None
    candidates = [(k, m) for k, m in evaluate(dict(summary), settings, now.hour,
                                              last_solar_at=last_solar,
                                              now=now.replace(tzinfo=None))
                  if k not in fired]
    candidates += [(k, m) for k, m in _degraded_streak(date, settings)
                   if k not in fired]
    candidates += _recoveries(dict(summary), fired)
    try:
        import home_tokens
        candidates += [(k, m) for k, m in home_tokens.alert_candidates()
                       if k not in fired]
    except Exception:  # noqa: BLE001 — best-effort
        log.exception("Home-token alert check failed")
    try:
        import solar_forecast
        candidates += [(k, m) for k, m in solar_forecast.alert_candidates()
                       if k not in fired]
    except Exception:  # noqa: BLE001 — best-effort
        log.exception("Solar under-performance check failed")
    sent_now = []
    for key, msg in candidates:
        # post_brief returns (ok, detail) and never raises; only a DELIVERED
        # alert is marked fired, so a Mattermost outage retries next tick
        # instead of silently losing the alert for the day.
        ok, detail = notifications.post_brief("### ⚠️ PowerSOC alert\n" + msg)
        if ok:
            fired.add(key)
            sent_now.append(key)
        else:
            log.warning("Alert delivery failed (%s): %s — will retry next tick",
                        key, detail)
    if sent_now:
        _mark(date, fired)
    return sent_now
