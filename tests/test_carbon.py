"""Tests for carbon-intensity parsing (HTTP mocked)."""
from __future__ import annotations

import carbon


def test_get_intensity_parse(monkeypatch):
    carbon._cache.update(ts=0, data=None)

    class R:
        def raise_for_status(self):
            pass

        def json(self):
            return {"data": {"shortname": "South Scotland", "data": [
                {"from": "2026-05-26T12:00Z", "to": "2026-05-26T12:30Z",
                 "intensity": {"forecast": 40, "index": "low"}},
                {"from": "2026-05-26T13:00Z", "to": "2026-05-26T13:30Z",
                 "intensity": {"forecast": 18, "index": "very low"}},
                {"from": "2026-05-26T18:00Z", "to": "2026-05-26T18:30Z",
                 "intensity": {"forecast": 95, "index": "high"}}]}}

    monkeypatch.setattr(carbon.requests, "get", lambda *a, **k: R())
    d = carbon.get_intensity()
    assert d["available"] is True and d["region"] == "South Scotland"
    assert d["current"]["g"] == 40 and d["current"]["index"] == "low"
    assert d["greenest"]["g"] == 18 and d["greenest"]["index"] == "very low"
    assert len(d["slots"]) == 3


def test_get_intensity_failure_soft(monkeypatch):
    carbon._cache.update(ts=0, data=None)

    def boom(*a, **k):
        raise carbon.requests.RequestException("offline")

    monkeypatch.setattr(carbon.requests, "get", boom)
    assert carbon.get_intensity()["available"] is False
