#!/usr/bin/env python3
"""Daily incremental sync entry point (run from cron at ~06:25 Europe/London).

Thin CLI wrapper around sync.run_daily_and_notify(). Fetches yesterday's data,
recomputes the daily summary, posts the morning brief, then (scheduled runs
only) refreshes tariff rates, writes the nightly backup, prunes old telemetry
and pings the optional heartbeat URL. Always exits 0 unless the process itself crashes — a missing
upstream is a degraded run, not a failure, per the no-silent-failures rule.

Usage:
    python sync_daily.py                # yesterday
    python sync_daily.py 2026-05-20     # a specific date (backfill one day)
    python sync_daily.py --tick         # one intraday round (hourly from cron)
"""
from __future__ import annotations

import logging
import sys

import config
import database as db
import sync

log = logging.getLogger("powersoc.sync_daily")


def main(argv: list[str]) -> int:
    config.configure_logging()
    config.ensure_dirs()
    db.init_db()

    if argv[1:2] == ["--tick"]:
        # Host installs: the catch-up, brief safety net and alerts the Docker
        # image runs in-process every hour.
        sync.intraday_tick()
        sync.heartbeat(sync.scheduler_ok())
        return 0
    date = argv[1] if len(argv) > 1 else None
    ok = True
    try:
        result = sync.run_daily_and_notify(date)
    except Exception:
        sync.heartbeat(False)
        raise
    if date is None:
        # Host/cron path had NO backups at all before — same housekeeping as
        # the in-app scheduler (backup to POWERSOC_BACKUP_DIR + retention).
        sync.refresh_tariff_rates()
        sync.daily_housekeeping()
        sync.heartbeat(ok and sync.scheduler_ok())
    log.info("Daily sync for %s: sunsynk=%s octopus=%s brief_sent=%s errors=%s",
             result["date"], result.get("sunsynk"), result.get("octopus"),
             result.get("brief_sent"), result.get("errors"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
