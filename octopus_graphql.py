"""Octopus Kraken GraphQL client — account balance + Octoplus Saving Sessions.

The REST API (octopus.py) covers consumption and tariffs. A few things only
live on the Kraken GraphQL API:

- **Account balance** (pence) — the running credit/debit on the account.
- **Octoplus Saving Sessions** — opt-in demand-flexibility events that pay
  OctoPoints per kWh saved. We surface upcoming events (and whether you've
  joined) so the dashboard/brief can nudge you to opt in and shift load.

Auth is a two-step dance: POST the REST API key to `obtainKrakenToken` to get
a short-lived JWT, then send that JWT as the `Authorization` header on queries.

Everything here is **best-effort and fails soft** — a GraphQL schema change or
an outage degrades to `{"available": False}` and never breaks the dashboard or
the morning brief. Field names follow the documented Kraken schema (as used by
the community Home Assistant integration); the defensive parsing means a schema
drift downgrades gracefully rather than throwing.

No live-API testing here (mock-only per project policy); see tests/test_graphql.py.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

import requests

import config

log = logging.getLogger("powersoc.octopus_graphql")

GRAPHQL_URL = "https://api.octopus.energy/v1/graphql/"
HTTP_TIMEOUT = 20
_TTL = 900        # 15 min — balance/sessions change slowly
_FAIL_TTL = 120   # re-stamp on failure so an outage doesn't retry per call
_TOKEN_TTL = 50 * 60   # Kraken JWTs last ~1 h; reuse conservatively
_lock = threading.Lock()
_cache: dict = {"ts": 0.0, "data": None, "fail_ts": 0.0}
_stmt_cache: dict = {"ts": 0.0, "data": None}
_token_cache: dict = {"ts": 0.0, "token": None, "key": None}

_TOKEN_MUTATION = """
mutation ($apiKey: String!) {
  obtainKrakenToken(input: { APIKey: $apiKey }) { token }
}
"""  # noqa: S105 - GraphQL query text, not a credential

_BALANCE_QUERY = """
query ($acc: String!) {
  account(accountNumber: $acc) { balance }
}
"""

_STATEMENTS_QUERY = """
query ($acc: String!) {
  account(accountNumber: $acc) {
    bills(first: 8) {
      edges {
        node {
          ... on StatementType {
            fromDate
            toDate
            totalCharges { grossTotal }
          }
        }
      }
    }
  }
}
"""

# NOTE (verified live 2026-05-28 against this account's Kraken API): the
# documented top-level `savingSessions` field is gone ("Did you mean
# 'appSessions'?"), and `octoplusAccountInfo` exposes only enrollment/
# eligibility — not session events. Saving sessions are also seasonal (winter).
# This query is kept for when the events path is re-identified; the feature
# stays gated off (POWERSOC_SAVING_SESSIONS) and fails soft until then.
_SAVING_SESSIONS_QUERY = """
query ($acc: String!) {
  savingSessions(accountNumber: $acc) {
    account(accountNumber: $acc) {
      hasJoinedCampaign
      joinedEvents { eventId }
    }
    events { id startAt endAt rewardPerKwhInOctoPoints }
  }
}
"""

class GraphQLError(Exception):
    pass


def _post(payload: dict, headers: dict | None = None) -> dict:
    r = requests.post(GRAPHQL_URL, json=payload,
                      headers={"User-Agent": "PowerSOC/1.0", **(headers or {})},
                      timeout=HTTP_TIMEOUT)
    if r.status_code >= 400:
        raise GraphQLError(f"HTTP {r.status_code}: {r.text[:200]}")
    body = r.json()
    if body.get("errors"):
        raise GraphQLError(str(body["errors"])[:200])
    return body.get("data") or {}


def _obtain_token(api_key: str) -> str:
    """Obtain (or reuse) a Kraken JWT. obtainKrakenToken is itself
    rate-limited, and the overview + statements paths used to mutate a fresh
    token each — share one per ~50 min instead."""
    now = time.time()
    with _lock:
        if (_token_cache["token"] and _token_cache["key"] == api_key
                and now - _token_cache["ts"] < _TOKEN_TTL):
            return _token_cache["token"]
    data = _post({"query": _TOKEN_MUTATION, "variables": {"apiKey": api_key}})
    token = ((data.get("obtainKrakenToken") or {}).get("token"))
    if not token:
        raise GraphQLError("no token returned from obtainKrakenToken")
    with _lock:
        _token_cache.update(ts=now, token=token, key=api_key)
    return token


def _drop_token() -> None:
    """Forget the cached JWT after a failure: a revoked/expired token would
    otherwise keep failing every query for the rest of its 50-minute TTL."""
    with _lock:
        _token_cache.update(ts=0.0, token=None)


def _parse_iso(s: str | None):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _parse_saving_sessions(data: dict) -> dict:
    """Parse a savingSessions payload into {joined_campaign, upcoming, next}.
    Tolerant of missing fields. `upcoming` is future events soonest-first."""
    ss = data.get("savingSessions") or {}
    acct = ss.get("account") or {}
    joined_ids = {j.get("eventId") for j in (acct.get("joinedEvents") or [])}
    now = datetime.now(timezone.utc)
    upcoming = []
    for e in ss.get("events") or []:
        start = _parse_iso(e.get("startAt"))
        if start and start > now:
            # Coerce to a number — this value is interpolated into the brief's
            # Mattermost markdown and the dashboard's innerHTML, so a string
            # from a schema drift must never pass through verbatim.
            try:
                pts = float(e.get("rewardPerKwhInOctoPoints"))
                pts = int(pts) if pts == int(pts) else pts
            except (TypeError, ValueError):
                pts = None
            upcoming.append({
                "id": e.get("id"), "start": e.get("startAt"), "end": e.get("endAt"),
                "points_per_kwh": pts,
                "joined": e.get("id") in joined_ids,
            })
    upcoming.sort(key=lambda x: x["start"] or "")
    return {"joined_campaign": bool(acct.get("hasJoinedCampaign")),
            "upcoming": upcoming, "next": upcoming[0] if upcoming else None}


def _build_overview(token: str, acc: str) -> dict:
    """Run the balance + saving-session queries independently so one failing
    doesn't lose the other."""
    auth = {"Authorization": token}
    out: dict = {"available": True}

    try:
        data = _post({"query": _BALANCE_QUERY, "variables": {"acc": acc}}, auth)
        bal_pence = (data.get("account") or {}).get("balance")
        if bal_pence is not None:
            out["balance_pence"] = bal_pence
            out["balance_gbp"] = round(bal_pence / 100.0, 2)
    except (GraphQLError, requests.RequestException, ValueError, TypeError) as e:
        log.warning("Kraken balance query failed: %s", e)

    # Octoplus Saving Sessions. The `savingSessions` field drifted on the live
    # API, so this is OFF unless POWERSOC_SAVING_SESSIONS=1 (and fails soft when
    # on). Balance + bill forecast are independent of it.
    if config.SAVING_SESSIONS_ENABLED:
        try:
            ssd = _post({"query": _SAVING_SESSIONS_QUERY, "variables": {"acc": acc}}, auth)
            out["saving_sessions"] = _parse_saving_sessions(ssd)
        except (GraphQLError, requests.RequestException, ValueError, TypeError, KeyError) as e:
            log.warning("Saving sessions query failed: %s", e)
            out["saving_sessions"] = {"joined_campaign": False, "upcoming": [],
                                      "next": None, "disabled_reason": "query failed"}
    else:
        out["saving_sessions"] = {
            "joined_campaign": False, "upcoming": [], "next": None,
            "disabled_reason": "disabled (set POWERSOC_SAVING_SESSIONS=1)"}

    return out


def get_account_overview(force: bool = False) -> dict:
    """Account balance + Octoplus saving sessions. Cached 15 min, fails soft.

    Returns `{"available": False}` when Octopus isn't configured or the API is
    unreachable — callers must treat every field as optional."""
    now = time.time()
    settings = config.Settings.load()
    if not settings.octopus_configured:
        return {"available": False, "configured": False}
    # Keyed by account: after an account change the old account's balance
    # was served for up to the cache TTL.
    if _cache.get("account") != settings.octopus_account_number:
        _cache.update(ts=0.0, data=None, fail_ts=0.0,
                      account=settings.octopus_account_number)
    if not force and _cache["data"] and now - _cache["ts"] < _TTL:
        return dict(_cache["data"])
    # Cold-cache outage: the negative TTL must apply even when there has never
    # been a success, or every page load re-attempts a blocking token POST.
    if not force and not _cache["data"] and now - _cache.get("fail_ts", 0.0) < _FAIL_TTL:
        return {"available": False, "configured": True}

    try:
        token = _obtain_token(settings.octopus_api_key)
        data = _build_overview(token, settings.octopus_account_number)
        data["fetched_at"] = datetime.now(timezone.utc).isoformat()
        _cache.update(ts=now, data=data)
        return dict(data)
    except (GraphQLError, requests.RequestException, ValueError, TypeError) as e:
        log.warning("Kraken account overview failed: %s", e)
        _drop_token()
        # Re-stamp with a short failure TTL so an outage doesn't re-attempt a
        # blocking token POST on every page load, and mark served data stale.
        _cache["ts"] = now - _TTL + _FAIL_TTL
        _cache["fail_ts"] = now
        if _cache["data"]:
            return {**_cache["data"], "stale": True}
        return {"available": False, "configured": True}


def get_latest_statements(force: bool = False) -> list[dict]:
    """Best-effort recent Octopus statement totals (Kraken GraphQL), newest
    first as {from, to, total_pence}. Returns [] on any error or schema
    mismatch — fails soft like get_account_overview. Cached 15 min (failures
    cached too, so a schema drift doesn't spam the API every reconcile call)."""
    now = time.time()
    settings = config.Settings.load()
    if not settings.octopus_configured:
        return []
    if _stmt_cache.get("account") != settings.octopus_account_number:
        _stmt_cache.update(ts=0.0, data=None, account=settings.octopus_account_number)
    if not force and _stmt_cache["data"] is not None and now - _stmt_cache["ts"] < _TTL:
        return list(_stmt_cache["data"])
    try:
        token = _obtain_token(settings.octopus_api_key)
        data = _post({"query": _STATEMENTS_QUERY,
                      "variables": {"acc": settings.octopus_account_number}},
                     {"Authorization": token})
        edges = (((data.get("account") or {}).get("bills") or {}).get("edges")) or []
        out = []
        for e in edges:
            node = (e or {}).get("node") or {}
            total = (node.get("totalCharges") or {}).get("grossTotal")
            if node.get("fromDate") and total is not None:
                out.append({"from": node["fromDate"], "to": node.get("toDate"),
                            "total_pence": total})
        _stmt_cache.update(ts=now, data=out)
        return list(out)
    except (GraphQLError, requests.RequestException, ValueError, TypeError, KeyError) as e:
        log.warning("Kraken statements query failed (schema may differ): %s", e)
        _drop_token()
        _stmt_cache.update(ts=now, data=[])   # cache the miss to avoid spamming
        return []
