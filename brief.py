"""Daily morning brief builder.

Assembles the Mattermost markdown summary from a day's daily_summaries row (or
a live computation). Always produces a message — when a source is missing the
relevant section is replaced with a degraded notice rather than omitted, so the
brief posts every day per the no-silent-failures rule.
"""
from __future__ import annotations

import logging
from datetime import date as date_cls
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import config
import database as db
import sync

LONDON = ZoneInfo("Europe/London")

log = logging.getLogger("powersoc.brief")


# ---------- formatting helpers --------------------------------------------

def _money(pence) -> str:
    # "–" for unknown: rendering a failed day as "£0.00" reads as "free",
    # contradicting the ⚠️ notice further up the brief.
    if pence is None:
        return "–"
    pounds = pence / 100.0
    return ("−£" if pounds < 0 else "£") + f"{abs(pounds):.2f}"


def _kwh(v) -> str:
    return "–" if v is None else f"{v:.1f} kWh"


def _pct(v) -> str:
    return "–" if v is None else f"{round(v)}%"


# ---------- notable events -------------------------------------------------

def detect_notable(summary: dict, gas_30d_avg: float | None = None) -> list[str]:
    """Return notable-event strings for a summary. Order: highlights first."""
    out: list[str] = []
    peak = summary.get("peak_soc_pct")
    end = summary.get("end_soc_pct")
    imp = summary.get("grid_import_kwh") or 0
    exp = summary.get("grid_export_kwh") or 0
    peak_imp = summary.get("peak_import_kwh") or 0
    gas = summary.get("gas_kwh") or 0

    if peak is not None and peak >= 99.5:
        out.append("🔋 Battery hit 100% SoC")
    if exp > imp and exp > 0:
        out.append("🌞 Net exporter — exported more than imported")
    if peak_imp > 2:
        out.append(f"⚡ High peak usage — {peak_imp:.1f} kWh in the peak window")
    if end is not None and end < 10:
        out.append("🪫 Battery nearly depleted at end of day")
    if gas_30d_avg and gas_30d_avg > 0 and gas > 1.5 * gas_30d_avg:
        out.append("🔥 High gas day — over 150% of the 30-day average")
    if not summary.get("sunsynk_ok"):
        out.append("⚠️ No data from Sunsynk")
    if not summary.get("octopus_ok"):
        out.append("⚠️ No data from Octopus")
    return out


def _gas_30d_avg(date: str) -> float | None:
    end = date_cls.fromisoformat(date) - timedelta(days=1)
    start = end - timedelta(days=29)
    rows = db.daily_summaries_range(start.isoformat(), end.isoformat())
    # `is not None`, NOT truthiness: real 0.0 days (gas off all summer) must
    # count, or the average inflates and the high-gas event never fires in
    # exactly the season an anomaly is most notable.
    vals = [r["gas_kwh"] for r in rows if r["gas_kwh"] is not None]
    return sum(vals) / len(vals) if vals else None


def _is_cosy_tariff() -> bool:
    """Whether the active import tariff is Cosy (persisted by run_daily).
    Defaults True (this deployment's tariff) when unknown."""
    try:
        import config
        import octopus
        code = config.get_text("import_tariff_code")
        return (not code) or octopus.is_cosy_product(code)
    except Exception:  # noqa: BLE001
        return True


# ---------- brief assembly -------------------------------------------------

def _today_outlook() -> list[str]:
    """A forward-looking 'Today' section: solar outlook + greenest grid window
    + Cosy reminder. Best-effort — returns [] if the data sources are down."""
    bits = []
    try:
        import weather
        fc = weather.get_forecast() or {}
        today_fc = fc.get("today") or {}
        if today_fc.get("outlook") and today_fc["outlook"] != "unknown":
            kwh = None
            try:
                import solar_forecast
                kwh = solar_forecast.forecast_kwh(fc).get("today_kwh")
            except Exception:  # noqa: BLE001, S110 — optional
                pass
            bits.append(f"Solar outlook **{today_fc['outlook']}**"
                        + (f" (~{kwh:.0f} kWh)" if kwh else "")
                        + (" (cached)" if fc.get("stale") else ""))
    except Exception:  # noqa: BLE001, S110 - best-effort; brief must never fail here
        pass
    try:
        import carbon
        gr = (carbon.get_intensity() or {}).get("greenest")
        if gr and gr.get("from"):
            # NESO times are UTC — convert to London or the brief is an hour
            # off for the ~7 BST months (planner.py already did this right).
            t = (datetime.fromisoformat(gr["from"].replace("Z", "+00:00"))
                 .astimezone(LONDON).strftime("%H:%M"))
            bits.append(f"greenest grid ~{t} (**{round(gr['g'])} gCO₂/kWh**)")
    except Exception:  # noqa: BLE001, S110 - best-effort; brief must never fail here
        pass
    if not bits:
        return []
    out = ["", "**🔆 Today**", "- " + " · ".join(bits)]
    if _is_cosy_tariff():
        out.append("- Cheap: 04–07, 13–16, 22–24 · avoid peak 16–19")
    return out


def _saving_session_section() -> list[str]:
    """Flag an Octoplus Saving Session starting within the next 48h, and nudge
    to opt in if not yet joined. Best-effort — returns [] when GraphQL is
    unavailable or there's no imminent session."""
    try:
        import octopus_graphql
        nxt = (octopus_graphql.get_account_overview()
               .get("saving_sessions") or {}).get("next")
        start = datetime.fromisoformat((nxt or {}).get("start", "").replace("Z", "+00:00"))
        if start - datetime.now(timezone.utc) > timedelta(hours=48):
            return []
        when = start.astimezone(LONDON).strftime("%a %d %b %H:%M")
        line = f"- **{when}**"
        if nxt.get("points_per_kwh"):
            line += f" · {nxt['points_per_kwh']} pts/kWh saved"
        line += " · ✅ joined" if nxt.get("joined") else " · ⚠️ **not joined — opt in**"
        return ["", "**🎁 Octoplus Saving Session**", line,
                "- Pre-charge the battery beforehand, then ride through on battery to maximise the reward."]
    except Exception:  # noqa: BLE001, S110 - best-effort; brief must never fail here
        return []


def _plan_section() -> list[str]:
    """One-line overnight battery charge recommendation (best-effort)."""
    try:
        import planner
        p = planner.get_plan()
        if not p.get("available") or not p.get("headline"):
            return []
        line = "- " + p["headline"]
        if p.get("charge_kwh") is not None:
            line += f" (~{p['charge_kwh']} kWh"
            if p.get("est_cost_pence") is not None:
                line += f", ≈{_money(p['est_cost_pence'])}"
            line += ")"
        return ["", "**🔌 Tonight's charge plan**", line]
    except Exception:  # noqa: BLE001, S110 - best-effort; brief must never fail
        return []


def _week_context(date: str, net_pence) -> list[str]:
    """One comparison line vs the trailing 7-day average net cost."""
    if net_pence is None:
        return []
    try:
        d = date_cls.fromisoformat(date)
        rows = db.daily_summaries_range((d - timedelta(days=7)).isoformat(),
                                        (d - timedelta(days=1)).isoformat())
        costs = [r["net_cost_pence"] for r in rows
                 if r["net_cost_pence"] is not None]
        if len(costs) < 3:
            return []
        avg = sum(costs) / len(costs)
        if avg <= 0:
            return []
        delta = (net_pence - avg) / avg * 100
        word = "above" if delta > 0 else "below"
        return [f"- vs 7-day avg ({_money(avg)}): **{abs(round(delta))}% {word}**"]
    except Exception:  # noqa: BLE001 — context line is best-effort
        return []


def _month_digest(date: str) -> list[str]:
    """Month roll-up appended to the brief for the LAST day of a month (which
    posts on the 1st): totals for cost, solar, export and import."""
    try:
        d = date_cls.fromisoformat(date)
        if (d + timedelta(days=1)).month == d.month:
            return []
        rows = db.daily_summaries_range(d.replace(day=1).isoformat(), date)
        if not rows:
            return []

        def tot(k):
            return sum((r[k] or 0) for r in rows)

        return ["", f"**📅 {d.strftime('%B')} digest**",
                f"- Net cost: **{_money(tot('net_cost_pence'))}** over "
                f"{len(rows)} days",
                f"- Solar: **{_kwh(tot('pv_kwh'))}** · Exported: "
                f"**{_kwh(tot('grid_export_kwh'))}** "
                f"(earned {_money(tot('export_earned_pence'))})",
                f"- Grid import: **{_kwh(tot('grid_import_kwh'))}** · Gas: "
                f"**{_kwh(tot('gas_kwh'))}**"]
    except Exception:  # noqa: BLE001 — digest is best-effort
        return []


def _config_warning() -> str | None:
    """A loud first line when data CAN'T be flowing. The brief is what a human
    reads daily; in 2026-10 three weeks went by with every source silently
    unset (undecryptable credentials) and nothing said so."""
    try:
        unreadable = config.unreadable_credentials()
        if unreadable:
            n = len(unreadable)
            return (f"> ⚠️ **{n} saved credential{'s' if n > 1 else ''} can't be "
                    "decrypted** (the encryption key changed) — re-enter "
                    "them in PowerSOC Settings.")
        s = config.Settings.load()
        if not (s.sunsynk_configured or s.octopus_configured):
            return ("> ⚠️ **No data source is configured** — add Sunsynk / "
                    "Octopus in PowerSOC Settings.")
        err = db.setting_get("last_backup_error")
        if err:
            return f"> ⚠️ **Last night's database backup failed** ({err})."
    except Exception:  # noqa: BLE001 — the brief must always build
        log.exception("Brief config check failed")
    return None


def build_daily_brief(date: str, flex_rate_p: float | None = None,
                      undelivered: dict | None = None) -> str:
    """Build the markdown brief for a date. Uses the stored summary if present,
    otherwise computes one live from intervals. `undelivered` names an earlier
    brief that never went out, so its absence is explained rather than silent."""
    row = db.get_daily_summary(date)
    s = dict(row) if row else sync.compute_daily_summary(date)

    nice_date = datetime.fromisoformat(date).strftime("%a %d %b %Y")
    lines = [f"### ⚡ PowerSOC — Energy Brief · {nice_date}", ""]
    warning = _config_warning()
    if warning:
        lines += [warning, ""]
    if undelivered:
        when = datetime.fromisoformat(undelivered["date"]).strftime("%a %d %b")
        lines += [f"> ⚠️ The brief for **{when}** wasn't delivered "
                  f"({undelivered.get('detail') or 'delivery failed'}) — see "
                  "PowerSOC Settings → Daily brief to resend it.", ""]

    # Solar & battery.
    if s.get("sunsynk_ok"):
        lines += [
            "**☀️ Solar & Battery**",
            f"- Generated: **{_kwh(s.get('pv_kwh'))}** · "
            f"Exported: **{_kwh(s.get('grid_export_kwh'))}**",
            f"- Battery: peak **{_pct(s.get('peak_soc_pct'))}** · "
            f"end-of-day **{_pct(s.get('end_soc_pct'))}**",
            f"- Discharge: **{_kwh(s.get('battery_discharge_kwh'))}** · "
            f"Charge: **{_kwh(s.get('battery_charge_kwh'))}**", ""]
    else:
        lines += ["**☀️ Solar & Battery** — ⚠️ Sunsynk data unavailable", ""]

    # Electricity. Show the section whenever we have ANY import figure —
    # Octopus settled, Octopus partial, or a Sunsynk CT estimate. Estimated
    # values are labelled (est.) so the reader knows accuracy is ~2% not exact.
    has_elec = s.get("octopus_ok") or s.get("import_estimated")
    if has_elec:
        cheap = s.get("cheap_import_kwh") or 0
        day = s.get("day_import_kwh") or 0
        peak = s.get("peak_import_kwh") or 0
        est = " (est. from CT)" if s.get("import_estimated") else ""
        cost_note = (est if s.get("import_cost_pence") is not None
                     else " (tariff rates unavailable)")
        elec_header = ("**🔌 Electricity (Cosy Octopus)**" if _is_cosy_tariff()
                       else "**🔌 Electricity**")
        lines += [
            elec_header,
            f"- Import: **{_kwh(s.get('grid_import_kwh'))}**{est} "
            f"(cheap {cheap:.1f} / day {day:.1f} / peak {peak:.1f})",
            f"- Import cost: **{_money(s.get('import_cost_pence'))}**{cost_note} · "
            f"Standing: **{_money(s.get('standing_elec_pence'))}**",
            f"- Export earned: **{_money(s.get('export_earned_pence'))}**"
            + (" (est.)" if s.get("export_estimated") else "")]
        saving = sync.saving_vs_flexible_pence(
            flex_rate_p, s.get("grid_import_kwh"), s.get("import_cost_pence"))
        if saving is not None:
            lines.append(f"- Est. saving vs Flexible (unshifted): **{_money(saving)}**")
        lines.append("")
    else:
        lines += ["**🔌 Electricity** — ⚠️ no data available", ""]

    # Gas (separate — no CT estimate available). Show it whenever gas intervals
    # were collected for the day, independent of the electricity feed (which is
    # what octopus_ok tracks) — so a gas-only day still reports gas.
    if db.intervals_for_date("gas_intervals", date):
        lines += [
            "**🔥 Gas**",
            f"- Used: **{_kwh(s.get('gas_kwh'))}**",
            f"- Cost: **{_money(s.get('gas_cost_pence'))}** · "
            f"Standing: **{_money(s.get('standing_gas_pence'))}**", ""]

    # Day total. Unknown stays unknown: a day with no (or unpriced) import
    # rendered "£0.00" here, contradicting the ⚠️ notice above it.
    elec_net = None
    if s.get("import_cost_pence") is not None and not (
            s.get("export_earned_pence") is None and s.get("grid_export_kwh")):
        elec_net = round(s["import_cost_pence"]
                         + (s.get("standing_elec_pence") or 0)
                         - (s.get("export_earned_pence") or 0), 2)
    lines += [
        "**📊 Day total**",
        f"- Net electricity cost: **{_money(elec_net)}** (after export credit)",
        f"- Total inc. gas: **{_money(s.get('net_cost_pence'))}**",
        f"- Self-sufficiency: **{_pct(s.get('self_sufficiency_pct'))}** 🌞"]
    lines += _week_context(date, s.get("net_cost_pence"))
    lines.append("")

    # Forward look — today's solar outlook + greenest grid window (best-effort).
    lines += _today_outlook()
    lines += _plan_section()
    lines += _saving_session_section()
    lines += _month_digest(date)

    # Partial-data note.
    elec_count = len(db.intervals_for_date("electricity_intervals", date))
    if s.get("data_partial"):
        expected = sync.expected_intervals(date)   # 46/48/50 (DST-aware)
        lines.append(f"⚠️ Octopus data partial ({elec_count}/{expected} "
                     f"intervals — may update later)")

    # Notable events (+ any tariff-watch notices since the previous brief).
    notable = detect_notable(s, _gas_30d_avg(date))
    try:
        import tariff_watch
        notable += tariff_watch.news_since(db.setting_get("last_brief_date"))
    except Exception:  # noqa: BLE001, S110 — best-effort
        pass
    if notable:
        lines += ["", "**Notable**"] + [f"- {n}" for n in notable]

    return "\n".join(lines)
