"""Self-calibrating solar forecast in kWh (feature 11).

Open-Meteo forecasts shortwave radiation (MJ/m²/day), which only says
"sunny" or "dull". This system's own history turns it into kWh: k = the
median of (PV kWh ÷ radiation) over recent days, so panel area, angle,
shading and inverter limits are all baked in. The same factor flags
under-performance: two days in a row well below what the sunshine should
have given (shading, a tripped isolator, a failed string)."""
from __future__ import annotations

import logging
import statistics
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests

import database as db

log = logging.getLogger("powersoc.solar_forecast")
LONDON = ZoneInfo("Europe/London")
_MIN_DAYS = 7
_UNDER = 0.6           # below 60% of expected …
_MIN_EXPECTED = 3.0    # … on days that should have given at least 3 kWh


def _today() -> date:
    return datetime.now(LONDON).date()


def store_radiation(values: dict[str, float | None]) -> int:
    rows = [(d, v) for d, v in values.items() if v is not None]
    with db.conn() as c:
        c.executemany("INSERT INTO weather_daily(date, radiation_mj) VALUES(?,?) "
                      "ON CONFLICT(date) DO UPDATE SET radiation_mj=excluded.radiation_mj", rows)
    return len(rows)


def radiation(d: str) -> float | None:
    with db.conn() as c:
        r = c.execute("SELECT radiation_mj FROM weather_daily WHERE date=?", (d,)).fetchone()
    return r["radiation_mj"] if r else None


def refresh_history(days: int = 60) -> int:
    """Fetch the last `days` of actual daily radiation (one request) and
    store it. Called once a day from the intraday tick. Fails soft."""
    import weather
    lat, lon, _ = weather._location()
    try:
        r = requests.get("https://api.open-meteo.com/v1/forecast", params={
            "latitude": lat, "longitude": lon, "timezone": "Europe/London",
            "past_days": days, "forecast_days": 1, "daily": "shortwave_radiation_sum"},
            headers={"User-Agent": "PowerSOC/1.0"}, timeout=10)
        r.raise_for_status()
        d = (r.json() or {}).get("daily") or {}
        today = _today().isoformat()
        vals = {t: v for t, v in zip(d.get("time") or [], d.get("shortwave_radiation_sum") or [])
                if t and t < today}
        return store_radiation(vals)
    except (requests.RequestException, ValueError) as e:
        log.warning("Radiation history fetch failed: %s", type(e).__name__)
        return 0


def calibration(days: int = 60) -> dict | None:
    """k (kWh per MJ/m²) from recent days with both PV and radiation."""
    end = _today()
    start = (end - timedelta(days=days)).isoformat()
    rad = {}
    with db.conn() as c:
        for r in c.execute("SELECT date, radiation_mj FROM weather_daily WHERE date>=? AND date<?",
                           (start, end.isoformat())):
            rad[r["date"]] = r["radiation_mj"]
    ratios = [r["pv_kwh"] / rad[r["date"]]
              for r in db.daily_summaries_range(start, (end - timedelta(days=1)).isoformat())
              if r["sunsynk_ok"] and r["pv_kwh"] is not None
              and (rad.get(r["date"]) or 0) >= 2.0]
    if len(ratios) < _MIN_DAYS:
        return None
    q = statistics.quantiles(ratios, n=4) if len(ratios) >= 4 else [min(ratios), 0, max(ratios)]
    return {"k": round(statistics.median(ratios), 4), "lo": round(q[0], 4),
            "hi": round(q[-1], 4), "days": len(ratios)}


def forecast_kwh(fc: dict) -> dict:
    """{today_kwh, tomorrow_kwh, …} from a weather.get_forecast() payload."""
    cal = calibration()

    def est(day):
        rad = ((fc or {}).get(day) or {}).get("radiation_mj")
        return round(cal["k"] * rad, 1) if cal and rad is not None else None

    def rng(day):
        rad = ((fc or {}).get(day) or {}).get("radiation_mj")
        return ([round(cal["lo"] * rad, 1), round(cal["hi"] * rad, 1)]
                if cal and rad is not None else None)
    return {"k": cal["k"] if cal else None, "calibration_days": cal["days"] if cal else 0,
            "today_kwh": est("today"), "tomorrow_kwh": est("tomorrow"),
            "today_range": rng("today"), "tomorrow_range": rng("tomorrow")}


def alert_candidates() -> list[tuple[str, str]]:
    """'solar_underperforming' when BOTH of the last two days produced under
    60% of what their sunshine should have given (one dull-looking day is
    usually the forecast/radiation, two is the system)."""
    cal = calibration()
    if not cal:
        return []
    days = [(_today() - timedelta(days=b)).isoformat() for b in (1, 2)]
    checks = []
    for d in days:
        s, rad = db.get_daily_summary(d), radiation(d)
        if not s or s["pv_kwh"] is None or rad is None:
            return []
        expected = cal["k"] * rad
        if expected < _MIN_EXPECTED or s["pv_kwh"] >= _UNDER * expected:
            return []
        checks.append((s["pv_kwh"], expected))
    pv, exp = checks[0]
    return [("solar_underperforming",
             f"☀️ Solar under-performing: **{pv:.1f} kWh** yesterday vs ~{exp:.0f} kWh "
             f"expected for the sunshine ({(pv / exp - 1) * 100:+.0f}%), two days running — "
             "check for shading, a tripped isolator or a string fault.")]
