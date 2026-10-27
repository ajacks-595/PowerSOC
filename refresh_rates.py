#!/usr/bin/env python3
"""Cron entry point: daily tariff-rate refresh (today's + tomorrow's periods).

Exists because `python -c "import sync; ..."` from cron puts cron's CWD (not
the app directory) on sys.path, so the import fails silently in the log. A
script invoked by absolute path puts ITS directory on sys.path — the same
mechanism sync_daily.py relies on.
"""
from __future__ import annotations

import config
import sync

if __name__ == "__main__":
    config.configure_logging()
    n = sync.refresh_tariff_rates()
    print(f"Tariff rates refreshed ({n} periods)", flush=True)
