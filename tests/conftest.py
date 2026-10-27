"""Shared pytest fixtures.

`_isolate` (autouse) enforces the project's hermeticity rules for EVERY test:
a fresh throwaway DB, no credential env vars (a developer's exported
MATTERMOST_WEBHOOK_URL must never let a "unit test" post to real Mattermost),
no leaked module-level caches, and a hard network kill switch — any un-mocked
HTTP raises requests.ConnectionError, which doubles as a realistic offline
simulation for the fail-soft paths.
"""
from __future__ import annotations

import pytest
import requests

import config
import database as db


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    # Fresh DB per test (previously opt-in via tmp_db — a test that forgot it
    # silently used the real data/powersoc.db).
    path = str(tmp_path / "test.db")
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db()

    # Scrub credential env vars so Settings.load() can never resolve real
    # credentials from the developer's environment.
    for key in config.CREDENTIAL_KEYS:
        monkeypatch.delenv(key, raising=False)
    # Backups land in the test's tmp dir (never the repo's data/), and no
    # heartbeat / pre-shared setup token leaks in from the developer's env.
    monkeypatch.setattr(config, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(config, "HEARTBEAT_URL", "")
    monkeypatch.setattr(config, "SETUP_TOKEN", "")

    # Reset module-level caches that otherwise leak between tests (what a
    # route returns must not depend on suite wall-clock speed or test order).
    import app as app_module
    import carbon
    import octopus_graphql as gql
    import weather
    import widgets
    with app_module._snap_meta_lock:
        app_module._snap_cache.clear()
    weather._cache.update(ts=0.0, data=None)
    weather._fc_cache.update(ts=0.0, data=None)
    weather._fail_until.update(current=0.0, forecast=0.0)
    carbon._cache.update(ts=0.0, data=None, fail_until=0.0)
    gql._cache.update(ts=0.0, data=None, fail_ts=0.0)
    gql._stmt_cache.update(ts=0.0, data=None)
    gql._token_cache.update(ts=0.0, token=None, key=None)
    widgets._cache.update(ts=0.0, date="", data=None)
    import home_tokens
    home_tokens.reset_state()
    import insights
    insights._capacity_cache.update(ts=0.0, kwh=None)
    import tariff_sim
    tariff_sim._state["running"] = False
    import sync
    with sync._backfill_lock:
        sync._backfill_state.update(running=False, days_done=0, days_total=0,
                                    skipped=0, error=None)

    # Network kill switch: tests must mock HTTP. Raising ConnectionError (not
    # an arbitrary exception) means un-mocked calls degrade exactly as a real
    # offline host would — fail-soft paths stay green, missing error handling
    # surfaces as a 500.
    def _no_network(self, method, url, *a, **k):
        raise requests.ConnectionError(
            f"network disabled in tests: {method} {url}")

    monkeypatch.setattr(requests.sessions.Session, "request", _no_network)

    # Retries still happen (the behaviour under test) but without real
    # exponential backoff: ~20 s of the 33 s suite was tenacity sleeping.
    import notifications
    import octopus
    import sunsynk
    for fn in (octopus.OctopusClient._get, sunsynk.SunsynkClient._get,
               notifications._post):
        monkeypatch.setattr(fn.retry, "sleep", lambda _s: None)
    yield


@pytest.fixture
def tmp_db(_isolate):
    """Kept for existing tests; isolation itself now happens in _isolate."""
    return db.DB_PATH
