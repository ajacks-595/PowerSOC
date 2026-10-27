"""Embeddable, no-auth widget pages (e.g. for Homarr / Homer dashboards).

These routes live under /widget/* which auth.py treats as public and app.py
serves with relaxed framing headers (no X-Frame-Options / frame-ancestors), so
they can be dropped into other dashboards via <iframe>. They are server-rendered
(no client API call) so they work without a session and fully air-gapped.
"""
from __future__ import annotations

import time

from flask import Blueprint, render_template, request

import config
import database as db
import sync

widgets_bp = Blueprint("widgets", __name__)

# Tiny TTL cache: this is a NO-AUTH endpoint refreshed by embedding dashboards
# every ~5 min — without it, an anonymous client could make every request
# recompute the full daily summary (8+ queries) for free.
_cache: dict = {"ts": 0.0, "date": "", "data": None}
_CACHE_TTL = 60.0


def _today_summary() -> dict:
    today = sync.today()
    now = time.time()
    if (_cache["data"] is not None and _cache["date"] == today
            and now - _cache["ts"] < _CACHE_TTL):
        return _cache["data"]
    row = db.get_daily_summary(today)
    data = dict(row) if row else sync.compute_daily_summary(today)
    _cache.update(ts=now, date=today, data=data)
    return data


@widgets_bp.route("/widget/today")
def widget_today():
    theme = request.args.get("theme", config.DEFAULT_THEME)
    if theme not in config.THEMES:
        theme = config.DEFAULT_THEME
    return render_template("widget_today.html",
                           theme=theme,
                           date=sync.today(),
                           s=_today_summary())
