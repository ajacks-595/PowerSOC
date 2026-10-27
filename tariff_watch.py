"""Tariff / agreement change watcher (quick win 8).

Runs after the daily rate refresh. Compared with the last snapshot it
reports: a newly booked agreement (e.g. a renewal starting next month), a
tariff ending within 30 days with nothing booked after it, and a change in
the unit prices of a fixed-window tariff (Cosy, Go, Flexible — not Agile or
Tracker, which change every day). Notices go to Mattermost straight away and
into the next daily brief. The first run only records a baseline."""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import config
import database as db

log = logging.getLogger("powersoc.tariff_watch")
LONDON = ZoneInfo("Europe/London")
_KEY = "tariff_watch"
_NEWS = "tariff_news"
_KINDS = ("import", "export", "gas")


def _load(key: str, default):
    try:
        return json.loads(db.setting_get(key) or "null") or default
    except (ValueError, TypeError):
        return default


def _nice(iso: str | None) -> str:
    if not iso:
        return "?"
    d = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    d = d.astimezone(LONDON) if d.tzinfo else d
    return f"{d.day} {d.strftime('%b %Y')}" if d.year != datetime.now(LONDON).year \
        else f"{d.day} {d.strftime('%b')}"


def _dynamic(code: str, rates: list[float]) -> bool:
    c = (code or "").upper()
    return "AGILE" in c or "TRACKER" in c or len(rates) > 6


def _today_rates(today: str) -> list[float]:
    return sorted({round(p["value_inc_vat"], 4) for p in db.tariff_periods_for_local_day(
        "electricity_import", date.fromisoformat(today)) if p["value_inc_vat"] is not None})


def check(meters, today: str | None = None) -> list[str]:
    import sync
    today = today or sync.today()
    snap = _load(_KEY, None)
    agreements = {k: sorted([a.get("tariff_code"), a.get("valid_from"), a.get("valid_to")]
                            for a in (meters.agreements or {}).get(k) or [])
                  for k in _KINDS}
    code = meters.import_tariff_code or ""
    rates = _today_rates(today)
    notes: list[str] = []
    if snap is None:
        snap = {"agreements": agreements, "rates": {}, "warned": []}
    else:
        for kind in _KINDS:
            seen = snap.get("agreements", {}).get(kind) or []
            for a in agreements[kind]:
                if a not in seen:
                    notes.append(f"📄 New {kind} agreement booked: **{a[0]}** from {_nice(a[1])}")
        noon = datetime.fromisoformat(today).replace(hour=12, tzinfo=LONDON)
        for kind in _KINDS:
            for a in agreements[kind]:
                frm, to = a[1], a[2]
                if not to:
                    continue
                f_dt = datetime.fromisoformat(frm.replace("Z", "+00:00")) if frm else None
                t_dt = datetime.fromisoformat(to.replace("Z", "+00:00"))
                active = (f_dt is None or f_dt <= noon) and t_dt > noon
                followed = any(b is not a and b[1] and
                               datetime.fromisoformat(b[1].replace("Z", "+00:00")) >= t_dt - timedelta(hours=1)
                               for b in agreements[kind])
                key = f"{kind}:{a[0]}:{to}"
                if active and not followed and t_dt - noon <= timedelta(days=30) \
                        and key not in snap.get("warned", []):
                    notes.append(f"⏳ Your {kind} tariff **{a[0]}** ends on {_nice(to)} "
                                 "with nothing booked after it — Octopus will move you to "
                                 "its standard variable tariff.")
                    snap.setdefault("warned", []).append(key)
        prev = (snap.get("rates") or {}).get(code)
        if code and rates and prev and prev != rates and not _dynamic(code, rates):
            fmt = lambda v: " / ".join(f"{x:g}" for x in v)  # noqa: E731
            notes.append(f"💷 **{code}** unit prices changed: {fmt(prev)} → {fmt(rates)} p/kWh")
    if code and rates and not _dynamic(code, rates):
        snap.setdefault("rates", {})[code] = rates
    snap["agreements"] = agreements
    db.setting_set(_KEY, json.dumps(snap))
    if notes:
        _record(today, notes)
    return notes


def _record(today: str, notes: list[str]) -> None:
    news = [n for n in _load(_NEWS, []) if n.get("date", "") >= (
        date.fromisoformat(today) - timedelta(days=30)).isoformat()]
    news += [{"date": today, "text": t} for t in notes]
    db.setting_set(_NEWS, json.dumps(news))
    for t in notes:
        log.info("Tariff watch: %s", t)
    if config.Settings.load().mattermost_configured:
        import notifications
        ok, detail = notifications.post_brief("### 🐙 PowerSOC tariff watch\n"
                                              + "\n".join(f"- {t}" for t in notes))
        if not ok:
            log.warning("Tariff notice not delivered: %s", detail)


def news_since(after: str | None) -> list[str]:
    """Notices recorded after `after` (a date), for the next brief."""
    return [n["text"] for n in _load(_NEWS, []) if not after or n.get("date", "") > after]


def recent_news(days: int = 30) -> list[dict]:
    return _load(_NEWS, [])[-20:]
