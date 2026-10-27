"""Tests for the Kraken GraphQL client — token, balance, saving sessions.

No live API calls: requests.post is mocked to return canned GraphQL bodies
keyed off the query string.
"""
from __future__ import annotations

import pytest

import config
import octopus_graphql as gql


class _Resp:
    def __init__(self, body, status=200):
        self._body = body
        self.status_code = status
        self.text = str(body)

    def json(self):
        return self._body


@pytest.fixture(autouse=True)
def _reset_cache():
    gql._cache.update(ts=0.0, data=None)
    gql._stmt_cache.update(ts=0.0, data=None)
    gql._token_cache.update(ts=0.0, token=None, key=None)
    yield
    gql._cache.update(ts=0.0, data=None)
    gql._stmt_cache.update(ts=0.0, data=None)
    gql._token_cache.update(ts=0.0, token=None, key=None)


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(config.Settings, "load", classmethod(lambda cls: type(
        "S", (), {"octopus_configured": True, "octopus_api_key": "sk_test",
                  "octopus_account_number": "A-1234"})()))


def _fake_post(events=None, balance=8523, joined_ids=None, token="jwt-tok"):
    """Build a requests.post double that routes by the query content."""
    joined = [{"eventId": e} for e in (joined_ids or [])]

    def post(url, json=None, headers=None, timeout=None):
        q = json["query"]
        if "obtainKrakenToken" in q:
            return _Resp({"data": {"obtainKrakenToken": {"token": token}}})
        if "balance" in q:
            return _Resp({"data": {"account": {"balance": balance}}})
        if "savingSessions" in q:
            return _Resp({"data": {"savingSessions": {
                "account": {"hasJoinedCampaign": bool(joined), "joinedEvents": joined},
                "events": events or []}}})
        return _Resp({"data": {}})
    return post


def test_overview_balance_and_disabled_sessions(monkeypatch, configured):
    # savingSessions parsing is disabled (live schema rejects the field);
    # the overview still returns balance + a disabled-stub saving_sessions.
    monkeypatch.setattr(gql.requests, "post", _fake_post())
    ov = gql.get_account_overview()
    assert ov["available"] is True
    assert ov["balance_pence"] == 8523
    assert ov["balance_gbp"] == 85.23
    ss = ov["saving_sessions"]
    assert ss["next"] is None
    assert ss["upcoming"] == []
    assert ss.get("disabled_reason")    # disabled by default (flag off)


def test_saving_sessions_when_enabled(monkeypatch, configured):
    monkeypatch.setattr(config, "SAVING_SESSIONS_ENABLED", True)

    def post(url, json=None, headers=None, timeout=None):
        q = json["query"]
        if "obtainKrakenToken" in q:
            return _Resp({"data": {"obtainKrakenToken": {"token": "t"}}})
        if "balance" in q:
            return _Resp({"data": {"account": {"balance": 100}}})
        if "savingSessions" in q:
            return _Resp({"data": {"savingSessions": {
                "account": {"hasJoinedCampaign": True, "joinedEvents": [{"eventId": "E2"}]},
                "events": [
                    {"id": "E1", "startAt": "2020-01-01T17:00:00Z",
                     "endAt": "2020-01-01T18:00:00Z", "rewardPerKwhInOctoPoints": 1800},
                    {"id": "E2", "startAt": "2099-01-01T17:00:00Z",
                     "endAt": "2099-01-01T18:00:00Z", "rewardPerKwhInOctoPoints": 2400},
                ]}}})
        return _Resp({"data": {}})
    monkeypatch.setattr(gql.requests, "post", post)
    ss = gql.get_account_overview()["saving_sessions"]
    assert ss["joined_campaign"] is True
    # Only the future event is 'upcoming' and is 'next', marked joined.
    assert len(ss["upcoming"]) == 1
    assert ss["next"]["id"] == "E2" and ss["next"]["joined"] is True
    assert ss["next"]["points_per_kwh"] == 2400


def test_not_configured_short_circuits(monkeypatch):
    monkeypatch.setattr(config.Settings, "load", classmethod(lambda cls: type(
        "S", (), {"octopus_configured": False, "octopus_api_key": "",
                  "octopus_account_number": ""})()))
    # Should never touch the network.
    def boom(*a, **k):
        raise AssertionError("network call when not configured")
    monkeypatch.setattr(gql.requests, "post", boom)
    ov = gql.get_account_overview()
    assert ov == {"available": False, "configured": False}


def test_balance_failure_soft(monkeypatch, configured):
    # Balance query errors; overview still returns (available, no balance key).
    def post(url, json=None, headers=None, timeout=None):
        q = json["query"]
        if "obtainKrakenToken" in q:
            return _Resp({"data": {"obtainKrakenToken": {"token": "t"}}})
        return _Resp({"errors": [{"message": "nope"}]}, status=200)
    monkeypatch.setattr(gql.requests, "post", post)
    ov = gql.get_account_overview()
    assert ov["available"] is True
    assert "balance_pence" not in ov   # balance failed, soft


def test_token_failure_fails_soft(monkeypatch, configured):
    def post(url, json=None, headers=None, timeout=None):
        return _Resp({"errors": [{"message": "bad key"}]})
    monkeypatch.setattr(gql.requests, "post", post)
    ov = gql.get_account_overview()
    assert ov == {"available": False, "configured": True}


def test_statements_parsed(monkeypatch, configured):
    def post(url, json=None, headers=None, timeout=None):
        q = json["query"]
        if "obtainKrakenToken" in q:
            return _Resp({"data": {"obtainKrakenToken": {"token": "t"}}})
        if "bills" in q:
            return _Resp({"data": {"account": {"bills": {"edges": [
                {"node": {"fromDate": "2026-04-01", "toDate": "2026-04-30",
                          "totalCharges": {"grossTotal": 8123}}},
                {"node": {"fromDate": "2026-03-01", "toDate": "2026-03-31",
                          "totalCharges": {"grossTotal": 9456}}},
            ]}}}})
        return _Resp({"data": {}})
    monkeypatch.setattr(gql.requests, "post", post)
    st = gql.get_latest_statements()
    assert len(st) == 2
    assert st[0] == {"from": "2026-04-01", "to": "2026-04-30", "total_pence": 8123}


def test_statements_schema_mismatch_fails_soft(monkeypatch, configured):
    def post(url, json=None, headers=None, timeout=None):
        if "obtainKrakenToken" in json["query"]:
            return _Resp({"data": {"obtainKrakenToken": {"token": "t"}}})
        return _Resp({"errors": [{"message": "Cannot query field 'bills'"}]})
    monkeypatch.setattr(gql.requests, "post", post)
    assert gql.get_latest_statements() == []


def test_cache_avoids_second_fetch(monkeypatch, configured):
    calls = {"n": 0}
    base = _fake_post()

    def counting(url, json=None, headers=None, timeout=None):
        calls["n"] += 1
        return base(url, json=json, headers=headers, timeout=timeout)
    monkeypatch.setattr(gql.requests, "post", counting)

    gql.get_account_overview()
    first = calls["n"]
    gql.get_account_overview()           # cached — no new calls
    assert calls["n"] == first
