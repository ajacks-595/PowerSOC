"""Tests for weather parsing + caching (HTTP mocked)."""
from __future__ import annotations

import weather


def test_parse_current():
    d = weather._parse({"temperature_2m": 11.4, "weather_code": 3,
                        "apparent_temperature": 9.1, "wind_speed_10m": 18,
                        "relative_humidity_2m": 82, "is_day": 1})
    assert d["available"] is True
    assert d["temp_c"] == 11.4
    assert d["condition"] == "Overcast"
    assert d["icon"] == "☁️"
    assert d["location"] == weather._DEFAULT_NAME


def test_parse_night_icon():
    d = weather._parse({"temperature_2m": 5, "weather_code": 0, "is_day": 0})
    assert d["icon"] == "🌙"        # clear-night
    assert d["condition"] == "Clear"


def test_parse_unknown_code():
    d = weather._parse({"temperature_2m": 5, "weather_code": 1234, "is_day": 1})
    assert d["condition"] == "—"


def test_solar_outlook_thresholds():
    assert weather.solar_outlook(20) == "high"
    assert weather.solar_outlook(10) == "moderate"
    assert weather.solar_outlook(3) == "low"
    assert weather.solar_outlook(None) == "unknown"


def test_get_forecast_parse(monkeypatch):
    weather._fc_cache.update(ts=0, data=None)

    class R:
        def raise_for_status(self):
            pass

        def json(self):
            return {"daily": {
                "time": ["2026-05-26", "2026-05-27"],
                "weather_code": [0, 3],
                "temperature_2m_max": [18.0, 15.0],
                "temperature_2m_min": [8.0, 7.0],
                "sunrise": ["2026-05-26T05:00", "2026-05-27T05:00"],
                "sunset": ["2026-05-26T21:00", "2026-05-27T21:00"],
                "shortwave_radiation_sum": [18.0, 4.0],
                "precipitation_probability_max": [10, 80]}}

    monkeypatch.setattr(weather.requests, "get", lambda *a, **k: R())
    f = weather.get_forecast()
    assert f["available"] is True
    assert f["today"]["outlook"] == "high" and f["today"]["condition"] == "Clear"
    assert f["tomorrow"]["outlook"] == "low" and f["tomorrow"]["precip_prob"] == 80


def test_get_weather_failure_is_soft(monkeypatch):
    weather._cache.update(ts=0, data=None)

    def boom(*a, **k):
        raise weather.requests.RequestException("offline")

    monkeypatch.setattr(weather.requests, "get", boom)
    d = weather.get_weather()
    assert d["available"] is False and d["location"] == weather._DEFAULT_NAME


def test_get_weather_error_body_does_not_clobber_cache(monkeypatch):
    """A 429/400 JSON error body must serve the LAST GOOD reading (stale),
    not cache an all-None 'available' payload over it."""
    import time
    good = {"available": True, "temp_c": 10.0, "location": "X"}
    # Expired (past the 30 min TTL) but recent enough to serve stale.
    weather._cache.update(ts=time.time() - 2400, data=dict(good))

    class R:
        def raise_for_status(self):
            raise weather.requests.HTTPError("429")

        def json(self):
            return {"error": True, "reason": "limited"}

    monkeypatch.setattr(weather.requests, "get", lambda *a, **k: R())
    d = weather.get_weather()
    assert d["temp_c"] == 10.0 and d.get("stale") is True
    assert weather._cache["data"]["temp_c"] == 10.0   # cache intact
