"""Local weather via Open-Meteo (free, no API key). Cached, degrades gracefully.

Location defaults to Dumfries, Scotland; override with POWERSOC_WEATHER_LAT /
_LON / _NAME. This is the one feature that reaches the internet at runtime; it
fails soft (returns the last value or an 'unavailable' marker) so it never
breaks the dashboard.
"""
from __future__ import annotations

import logging
import os
import threading
import time

import requests

log = logging.getLogger("powersoc.weather")

_DEFAULT_LAT, _DEFAULT_LON, _DEFAULT_NAME = "55.0701", "-3.6033", "Dumfries"
_TTL = 1800   # 30 min cache
_cache: dict = {"ts": 0.0, "data": None}
_FC_TTL = 3600   # forecast cache 1h
_fc_cache: dict = {"ts": 0.0, "data": None}
_TIMEOUT = 6
# After a failed fetch, don't retry this feed for a while: an Open-Meteo
# outage used to be re-requested (10 s timeout) on every page load.
_FAIL_TTL = 600
_fail_until = {"current": 0.0, "forecast": 0.0}
# Current conditions older than this are not shown as "now", even stale.
_STALE_MAX_S = 3 * 3600
# One in-flight fetch per feed. Acquired WITHOUT blocking: a single shared
# lock held across a slow request queued every other thread behind it —
# with gunicorn's few threads, /api/health included.
_cur_lock = threading.Lock()
_fc_lock = threading.Lock()


def _fallback(cache: dict, name: str, max_age: float | None = None,
              valid=None) -> dict:
    """Stale cached data when still meaningful, else 'unavailable'."""
    data = cache["data"]
    if data and (max_age is None or time.time() - cache["ts"] < max_age) \
            and (valid is None or valid(data)):
        return {**data, "stale": True}
    return {"location": name, "available": False}


def _forecast_is_current(data: dict) -> bool:
    """A stale forecast is only usable while its 'today' IS today."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    today = datetime.now(ZoneInfo("Europe/London")).date().isoformat()
    return ((data.get("today") or {}).get("date") or "") == today


def _location() -> tuple[float, float, str]:
    """Location resolved at call time: Settings UI first, then env, then the
    Dumfries default. Malformed values degrade to the default (a bad override
    must not turn every weather endpoint into a 500)."""
    import config
    lat_s = config.get_text("weather_lat",
                            os.environ.get("POWERSOC_WEATHER_LAT", _DEFAULT_LAT))
    lon_s = config.get_text("weather_lon",
                            os.environ.get("POWERSOC_WEATHER_LON", _DEFAULT_LON))
    name = config.get_text("weather_name",
                           os.environ.get("POWERSOC_WEATHER_NAME", _DEFAULT_NAME))
    try:
        return float(lat_s), float(lon_s), name
    except (TypeError, ValueError):
        log.warning("Malformed weather lat/lon (%r, %r) — using default "
                    "location", lat_s, lon_s)
        return float(_DEFAULT_LAT), float(_DEFAULT_LON), name

# WMO weather codes -> (description, day emoji, night emoji)
_WMO = {
    0: ("Clear", "☀️", "🌙"), 1: ("Mainly clear", "🌤️", "🌙"),
    2: ("Partly cloudy", "⛅", "☁️"), 3: ("Overcast", "☁️", "☁️"),
    45: ("Fog", "🌫️", "🌫️"), 48: ("Rime fog", "🌫️", "🌫️"),
    51: ("Light drizzle", "🌦️", "🌧️"), 53: ("Drizzle", "🌦️", "🌧️"),
    55: ("Heavy drizzle", "🌧️", "🌧️"),
    61: ("Light rain", "🌦️", "🌧️"), 63: ("Rain", "🌧️", "🌧️"),
    65: ("Heavy rain", "🌧️", "🌧️"),
    71: ("Light snow", "🌨️", "🌨️"), 73: ("Snow", "🌨️", "🌨️"),
    75: ("Heavy snow", "❄️", "❄️"), 77: ("Snow grains", "🌨️", "🌨️"),
    80: ("Rain showers", "🌦️", "🌧️"), 81: ("Rain showers", "🌧️", "🌧️"),
    82: ("Violent showers", "⛈️", "⛈️"),
    85: ("Snow showers", "🌨️", "🌨️"), 86: ("Snow showers", "❄️", "❄️"),
    95: ("Thunderstorm", "⛈️", "⛈️"),
    96: ("Thunderstorm + hail", "⛈️", "⛈️"), 99: ("Thunderstorm + hail", "⛈️", "⛈️"),
}


def get_weather() -> dict:
    now = time.time()
    if _cache["data"] and now - _cache["ts"] < _TTL:
        return _cache["data"]
    lat, lon, name = _location()
    if now < _fail_until["current"] or not _cur_lock.acquire(blocking=False):
        return _fallback(_cache, name, _STALE_MAX_S)
    try:
        if _cache["data"] and time.time() - _cache["ts"] < _TTL:
            return _cache["data"]
        r = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={"latitude": lat, "longitude": lon,
                    "timezone": "Europe/London",
                    "current": "temperature_2m,weather_code,wind_speed_10m,"
                               "relative_humidity_2m,apparent_temperature,is_day"},
            headers={"User-Agent": "PowerSOC/1.0"}, timeout=_TIMEOUT)
        # Without this, a 400/429 JSON error body parsed as an all-None
        # "available" reading and CLOBBERED the last good cached value.
        r.raise_for_status()
        data = _parse((r.json() or {}).get("current", {}), name)
        _cache.update(ts=now, data=data)
        _fail_until["current"] = 0.0
        return data
    except (requests.RequestException, ValueError) as e:
        log.warning("Weather fetch failed: %s", type(e).__name__)
        _fail_until["current"] = time.time() + _FAIL_TTL
        return _fallback(_cache, name, _STALE_MAX_S)
    finally:
        _cur_lock.release()


def solar_outlook(radiation_mj) -> str:
    """Qualitative solar outlook from Open-Meteo shortwave_radiation_sum
    (MJ/m²/day). UK ranges ~2 (dull winter) to ~25 (clear summer)."""
    if radiation_mj is None:
        return "unknown"
    if radiation_mj >= 14:
        return "high"
    if radiation_mj >= 7:
        return "moderate"
    return "low"


def get_forecast() -> dict:
    """Today + tomorrow daily forecast (temps, sunrise/sunset, solar outlook).
    Cached 1h; fails soft, backs off after a failure, and never serves a
    stale forecast whose "today" is a previous day."""
    now = time.time()
    if _fc_cache["data"] and now - _fc_cache["ts"] < _FC_TTL:
        return _fc_cache["data"]
    lat, lon, name = _location()
    if now < _fail_until["forecast"] or not _fc_lock.acquire(blocking=False):
        return _fallback(_fc_cache, name, valid=_forecast_is_current)
    try:
        if _fc_cache["data"] and time.time() - _fc_cache["ts"] < _FC_TTL:
            return _fc_cache["data"]
        r = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={"latitude": lat, "longitude": lon,
                    "timezone": "Europe/London",
                    "forecast_days": 2,
                    "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
                             "sunrise,sunset,shortwave_radiation_sum,"
                             "precipitation_probability_max"},
            headers={"User-Agent": "PowerSOC/1.0"}, timeout=_TIMEOUT)
        r.raise_for_status()
        d = (r.json() or {}).get("daily", {})

        def day(i):
            code = d.get("weather_code", [None, None])[i]
            desc = _WMO.get(int(code), ("—",))[0] if code is not None else "—"
            rad = d.get("shortwave_radiation_sum", [None, None])[i]
            # precip_prob feeds an innerHTML sink client-side — coerce.
            pp = d.get("precipitation_probability_max", [None, None])[i]
            try:
                pp = int(pp) if pp is not None else None
            except (TypeError, ValueError):
                pp = None
            return {"date": d.get("time", [None, None])[i],
                    "temp_max": d.get("temperature_2m_max", [None, None])[i],
                    "temp_min": d.get("temperature_2m_min", [None, None])[i],
                    "sunrise": d.get("sunrise", [None, None])[i],
                    "sunset": d.get("sunset", [None, None])[i],
                    "precip_prob": pp,
                    "radiation_mj": rad, "outlook": solar_outlook(rad),
                    "condition": desc}
        data = {"available": True, "location": name,
                "today": day(0), "tomorrow": day(1)}
        _fc_cache.update(ts=now, data=data)
        _fail_until["forecast"] = 0.0
        return data
    except (requests.RequestException, ValueError, IndexError, KeyError) as e:
        log.warning("Forecast fetch failed: %s", type(e).__name__)
        _fail_until["forecast"] = time.time() + _FAIL_TTL
        return _fallback(_fc_cache, name, valid=_forecast_is_current)
    finally:
        _fc_lock.release()


def _parse(cur: dict, name: str = _DEFAULT_NAME) -> dict:
    code = int(cur.get("weather_code", -1)) if cur.get("weather_code") is not None else -1
    desc, day_icon, night_icon = _WMO.get(code, ("—", "🌡️", "🌡️"))
    is_day = cur.get("is_day", 1)
    return {
        "available": True,
        "location": name,
        "temp_c": cur.get("temperature_2m"),
        "feels_c": cur.get("apparent_temperature"),
        "condition": desc,
        "icon": day_icon if is_day else night_icon,
        "wind_kmh": cur.get("wind_speed_10m"),
        "humidity": cur.get("relative_humidity_2m"),
        "is_day": bool(is_day),
    }
