"""Settings, user management, 2FA/session self-service, brief test and
backfill — registered on routes.api.api_bp (admin gating is path-based in
auth._is_admin_only)."""
from __future__ import annotations

import logging
from datetime import date as date_cls

import auth
import config
import database as db
import sync
from routes.api import api_bp
from routes.common import _body_str, _cache_clear, _check_webhook, _json_body, err, ok

log = logging.getLogger("powersoc.admin")


@api_bp.route("/settings")
def settings_get():
    """Current settings: which credentials are set (secrets masked), discovered
    meters, and the default theme."""
    creds = {}
    unreadable = config.unreadable_credentials()
    for key in config.CREDENTIAL_KEYS:
        val = config.get_credential(key)
        if key in config.SECRET_KEYS:
            creds[key] = {"set": bool(val), "secret": True, "value": None}
        else:
            creds[key] = {"set": bool(val), "secret": False, "value": val}
        # Stored but undecryptable: the UI must not show it as merely empty.
        creds[key]["unreadable"] = key in unreadable
    return ok({"credentials": creds, "unreadable": unreadable,
               "default_theme": config.DEFAULT_THEME,
               "themes": list(config.THEMES)})


@api_bp.route("/settings/credentials", methods=["POST"])
def settings_save_credentials():
    body = _json_body()
    changed = []
    _check_webhook(body.get("MATTERMOST_WEBHOOK_URL")
                   if isinstance(body.get("MATTERMOST_WEBHOOK_URL"), str) else None)
    # Validate EVERYTHING before writing anything: a bad field part-way used to
    # leave the earlier ones saved, behind a 400 and with no audit entry.
    for key, value in body.items():
        # Non-string JSON (numbers, arrays) used to crash encrypt() with a 500.
        if key in config.CREDENTIAL_KEYS and value is not None \
                and not isinstance(value, str):
            return err(f"{key} must be a string", 400)
    for key, value in body.items():
        if key not in config.CREDENTIAL_KEYS:
            continue
        # Blank secret = "leave unchanged" (the form never echoes secrets back).
        if value == "" and key in config.SECRET_KEYS:
            continue
        config.set_credential(key, value or None)
        changed.append(key)
    if changed:
        # Snapshots built before the save (e.g. a cached "not configured"
        # in-depth payload, TTL 1h) must not outlive a credential change.
        _cache_clear()
        if any(k.startswith("OCTOPUS_") for k in changed):
            sync.clear_meter_cache()
            import octopus_graphql
            octopus_graphql._cache.update(ts=0.0, data=None, fail_ts=0.0)
            octopus_graphql._stmt_cache.update(ts=0.0, data=None)
    auth.audit("settings.credentials", details={"keys": changed})
    return ok({"changed": changed})


@api_bp.route("/settings/test/sunsynk", methods=["POST"])
def settings_test_sunsynk():
    import sunsynk
    body = _json_body()
    s = config.Settings.load()
    client = sunsynk.SunsynkClient(
        username=_body_str(body, "SUNSYNK_USERNAME") or s.sunsynk_username,
        password=_body_str(body, "SUNSYNK_PASSWORD") or s.sunsynk_password,
        plant_id=_body_str(body, "SUNSYNK_PLANT_ID") or s.sunsynk_plant_id)
    try:
        return ok(client.test_connection())
    except sunsynk.SunsynkError as e:
        return err(f"Sunsynk connection failed: {e}", 502)
    finally:
        client.close()   # previously leaked on the failure path


@api_bp.route("/settings/discover", methods=["POST"])
def settings_discover():
    import octopus
    body = _json_body()
    s = config.Settings.load()
    octo = None
    account = _body_str(body, "OCTOPUS_ACCOUNT_NUMBER") or s.octopus_account_number
    try:
        octo = octopus.OctopusClient(
            _body_str(body, "OCTOPUS_API_KEY") or s.octopus_api_key, account)
        info = octo.discover_meters()
    except octopus.OctopusError as e:
        return err(f"Discovery failed: {e}", 502)
    finally:
        if octo is not None:
            octo.close()
    _cache_clear()
    sync._store_discovery(account, info)
    # Persist the discovered meter identifiers (not the tariff codes — those are
    # re-resolved at sync time).
    mapping = {
        "OCTOPUS_MPAN": info.import_mpan, "OCTOPUS_METER_SERIAL": info.import_serial,
        "OCTOPUS_EXPORT_MPAN": info.export_mpan,
        "OCTOPUS_EXPORT_SERIAL": info.export_serial,
        "OCTOPUS_MPRN": info.mprn, "OCTOPUS_GAS_SERIAL": info.gas_serial,
    }
    for k, v in mapping.items():
        if v:
            config.set_credential(k, v)
    auth.audit("settings.discover")
    from dataclasses import asdict
    return ok(asdict(info))


@api_bp.route("/settings/test/webhook", methods=["POST"])
def settings_test_webhook():
    import notifications
    body = _json_body()
    url = _body_str(body, "MATTERMOST_WEBHOOK_URL")
    _check_webhook(url)
    sent, detail = notifications.test_webhook(
        webhook_url=url, channel=_body_str(body, "MATTERMOST_CHANNEL"))
    return ok({"sent": sent, "detail": detail})


@api_bp.route("/settings/system")
def settings_system_get():
    return ok({
        "system_cost_gbp": config.get_number("system_cost_gbp"),
        "battery_nameplate_kwh": config.get_number("battery_nameplate_kwh"),
        "battery_rated_cycles": config.get_number("battery_rated_cycles", 6000),
        "battery_reserve_pct": config.get_number("battery_reserve_pct", 10),
        "battery_charge_kw": config.get_number("battery_charge_kw"),
        "gas_units": config.get_text("gas_units", "m3"),
        "weather_lat": config.get_text("weather_lat"),
        "weather_lon": config.get_text("weather_lon"),
        "weather_name": config.get_text("weather_name"),
        "carbon_outcode": config.get_text("carbon_outcode"),
    })


@api_bp.route("/settings/system", methods=["POST"])
def settings_system_set():
    """Validate every field first, then apply them all — a bad field part-way
    used to leave the earlier ones saved behind a 400."""
    body = _json_body()
    numbers: dict = {}
    texts: dict = {}
    for k in ("system_cost_gbp", "battery_nameplate_kwh", "battery_rated_cycles",
              "battery_reserve_pct", "battery_charge_kw"):
        if k in body:
            v = body[k]
            if v not in (None, "") and not isinstance(v, (int, float, str)):
                return err(f"{k} must be a number", 400)
            try:
                # Finite and non-negative: "nan"/"inf" used to be stored and
                # turned /api/roi into invalid JSON or a permanent 500.
                if v not in (None, "") and config.parse_finite(v) < 0:
                    return err(f"{k} must not be negative", 400)
            except (TypeError, ValueError):
                return err(f"{k} must be a finite number", 400)
            if k == "battery_reserve_pct" and v not in (None, "") \
                    and config.parse_finite(v) > 100:
                return err("battery_reserve_pct must be 0–100", 400)
            numbers[k] = v
    if "gas_units" in body:
        gu = (body.get("gas_units") or "m3")
        if not isinstance(gu, str) or gu.lower() not in ("m3", "kwh"):
            return err("gas_units must be 'm3' or 'kwh'", 400)
        texts["gas_units"] = gu.lower()
    for k, lim in (("weather_lat", 90), ("weather_lon", 180)):
        if k in body:
            v = body[k]
            if v not in (None, ""):
                try:
                    f = config.parse_finite(v)
                except (TypeError, ValueError):
                    return err(f"{k} must be a finite number", 400)
                if abs(f) > lim:
                    return err(f"{k} must be within ±{lim}", 400)
            texts[k] = str(v) if v not in (None, "") else None
    for k in ("weather_name", "carbon_outcode"):
        if k in body and body[k] is not None and not isinstance(body[k], str):
            return err(f"{k} must be a string", 400)
    if "weather_name" in body:
        texts["weather_name"] = (body["weather_name"] or "").strip()[:64] or None
    outcode = None
    if "carbon_outcode" in body:
        import carbon
        outcode = carbon.normalise_outcode(body["carbon_outcode"] or "")
        # The API answers a bare area ("DG") with `null`: require a district.
        if outcode and not carbon.OUTCODE_RE.match(outcode):
            return err("carbon_outcode must be a postcode district such as "
                       "DG1 (the first half of your postcode)", 400)

    # --- apply (everything above validated) ---
    for k, v in numbers.items():
        config.set_number(k, v)
    moved = any(k in texts and (texts[k] or "") != (config.get_text(k) or "")
                for k in ("weather_lat", "weather_lon"))
    for k, v in texts.items():
        config.set_text(k, v)
    if moved:
        # A new location must not keep serving the old place's weather and
        # solar forecast until the cache TTL runs out.
        import weather
        weather._cache.update(ts=0.0, data=None)
        weather._fc_cache.update(ts=0.0, data=None)
        weather._fail_until.update(current=0.0, forecast=0.0)
    if outcode is not None and outcode != (config.get_text("carbon_outcode") or ""):
        import carbon
        config.set_text("carbon_outcode", outcode or None)
        carbon._cache.update(ts=0.0, data=None, fail_until=0.0)
    auth.audit("settings.system", details={"keys": sorted(
        set(numbers) | set(texts) | ({"carbon_outcode"} if outcode is not None else set()))})
    return ok({"saved": True})


@api_bp.route("/settings/env-export")
def settings_env_export():
    from flask import Response
    # This returns ALL credentials decrypted in plaintext — record who pulled it.
    auth.audit("settings.env_export")
    lines = ["# PowerSOC .env (exported from Settings)"]
    for key in config.CREDENTIAL_KEYS:
        lines.append(f"{key}={config.get_credential(key)}")
    lines += [f"POWERSOC_PORT={config.LISTEN_PORT}",
              f"POWERSOC_DATA_DIR={config.DATA_DIR}",
              f"POWERSOC_COOKIE_SECURE={'1' if config.COOKIE_SECURE else '0'}",
              "TZ=Europe/London"]
    return Response("\n".join(lines) + "\n", mimetype="text/plain")


# ---------- home API token management (session-authed) ----------

@api_bp.route("/settings/home-token")
def home_token_status():
    return ok({"enabled": auth.home_api_enabled()})


@api_bp.route("/settings/home-token/generate", methods=["POST"])
def home_token_generate():
    token = auth.home_api_generate_token()
    auth.audit("home_token.generate")
    return ok({"token": token})   # shown once


@api_bp.route("/settings/home-token", methods=["DELETE"])
def home_token_clear():
    auth.home_api_token_set(None)
    auth.audit("home_token.clear")
    return ok({"enabled": False})


# ---------- alerts + heartbeat (quick win 3) ----------

@api_bp.route("/settings/alerts")
def settings_alerts_get():
    a = config.alert_config()
    return ok({"enabled": a["enabled"], "battery_low_pct": a["battery_low_pct"],
               "high_cost_gbp": round(a["high_cost_pence"] / 100, 2),
               "peak_kwh": a["peak_kwh"], "quiet_hours": a["quiet_hours"],
               "heartbeat_set": bool(config.heartbeat_url())})


@api_bp.route("/settings/alerts", methods=["POST"])
def settings_alerts_set():
    """Validate everything, then apply (like Settings → System)."""
    import re

    import notifications
    body = _json_body()
    nums = {}
    for key, store, lo, hi, scale in (
            ("battery_low_pct", "alert_battery_low_pct", 0, 100, 1),
            ("high_cost_gbp", "alert_high_cost_pence", 0, 10_000, 100),
            ("peak_kwh", "alert_peak_kwh", 0, 1_000, 1)):
        if key in body and body[key] not in (None, ""):
            try:
                v = config.parse_finite(body[key])
            except (TypeError, ValueError):
                return err(f"{key} must be a finite number", 400)
            if not lo <= v <= hi:
                return err(f"{key} must be between {lo} and {hi}", 400)
            nums[store] = v * scale
        elif key in body:
            nums[store] = None
    quiet = body.get("quiet_hours")
    if quiet not in (None, "") and not (
            isinstance(quiet, str) and re.fullmatch(r"([01]?\d|2[0-3])-([01]?\d|2[0-3])", quiet.strip())):
        return err("quiet_hours must look like 22-7 (local hours) or be blank", 400)
    hb = body.get("heartbeat_url")
    if hb not in (None, "") and not notifications.valid_webhook_url(hb):
        return err("heartbeat_url must be an http(s) URL", 400)
    # --- apply ---
    if "enabled" in body:
        config.set_text("alerts_enabled", "1" if body["enabled"] else "0")
    for store, v in nums.items():
        config.set_number(store, v)
    if "quiet_hours" in body:
        config.set_text("alert_quiet_hours", (quiet or "").strip() or None)
    if hb not in (None, ""):
        config.set_credential("HEARTBEAT_URL", hb.strip())
    elif body.get("heartbeat_clear"):
        config.set_credential("HEARTBEAT_URL", None)
    auth.audit("settings.alerts", details={"keys": sorted(k for k in body if k != "heartbeat_url")
                                           + (["heartbeat_url"] if hb else [])})
    return ok({"saved": True})


@api_bp.route("/settings/alerts/test-heartbeat", methods=["POST"])
def settings_alerts_test_heartbeat():
    """Ping the heartbeat URL once (success ping) and report the outcome —
    never the URL (it is a capability)."""
    import requests as _rq
    url = config.heartbeat_url()
    if not url:
        return err("no heartbeat URL set", 400)
    try:
        r = _rq.get(url, timeout=10, headers={"User-Agent": "PowerSOC"})
        code = getattr(r, "status_code", 0)
        return ok({"ok": 200 <= code < 300, "status": code})
    except _rq.RequestException as e:
        return ok({"ok": False, "status": None, "error": type(e).__name__})


# ---------- per-consumer home-API tokens (O1) ----------

@api_bp.route("/settings/home-consumers")
def home_consumers_list():
    import home_tokens
    return ok({"consumers": home_tokens.list_consumers(),
               "rejections": home_tokens.rejections(),
               "enabled": home_tokens.any_configured()})


@api_bp.route("/settings/home-consumers", methods=["POST"])
def home_consumers_create():
    import re

    import home_tokens
    name = (_body_str(_json_body(), "name") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9 ._()\-]{1,40}", name):
        return err("name: 1–40 letters, digits, space . _ ( ) -", 400)
    made = home_tokens.create(name)
    auth.audit("home_token.create", "home_consumer", made["id"], {"name": name})
    return ok(made)   # the token is shown ONCE


@api_bp.route("/settings/home-consumers/<cid>", methods=["DELETE"])
def home_consumers_revoke(cid):
    import home_tokens
    if cid != home_tokens.LEGACY_ID and not cid.isdigit():
        return err("unknown consumer", 404)
    home_tokens.revoke(cid)
    auth.audit("home_token.revoke", "home_consumer", cid)
    return ok({"revoked": cid})


# ---------- per-account 2FA + session revocation (self-service) ----------

@api_bp.route("/2fa/status")
def twofa_status():
    u = auth.current_user()
    return ok({"enabled": bool(u and u.get("totp_secret"))})   # set ⇒ on


def _current_code_ok(u, body) -> bool:
    """A current authenticator code for an account that already has 2FA —
    single-use, like at login."""
    import totp
    counter = totp.verify_counter(auth.totp_secret_of(u) or "",
                                  _body_str(body, "code") or "")
    return counter is not None and db.claim_totp_counter(u["id"], counter)


@api_bp.route("/2fa/setup", methods=["POST"])
def twofa_setup():
    from flask import session as _s

    import totp
    u = auth.current_user()
    # Replacing an ACTIVE second factor needs that factor: otherwise a stolen
    # session could enrol its own authenticator and lock the owner out.
    if u.get("totp_secret") and not _current_code_ok(u, _json_body()):
        return err("enter a current authenticator code to replace 2FA")
    secret = totp.generate_secret()
    _s["pending_totp"] = secret    # held until confirmed below, then stored
    return ok({"secret": secret,
               "otpauth_uri": totp.provisioning_uri(secret, u["username"])})


@api_bp.route("/2fa/enable", methods=["POST"])
def twofa_enable():
    from flask import session as _s

    import totp
    u = auth.current_user()
    secret = _s.get("pending_totp")
    code = _body_str(_json_body(), "code") or ""
    counter = totp.verify_counter(secret or "", code)
    if not secret or counter is None:
        return err("invalid or expired code — start setup again")
    auth.store_totp_secret(u["id"], secret)   # Fernet-encrypted at rest
    # The confirming code is spent: it must not also work at the next login.
    db.set_totp_last_counter(u["id"], counter)
    _s.pop("pending_totp", None)
    # One-time recovery codes (shown ONCE): a lost authenticator otherwise
    # means manual DB surgery. Stored hashed; any code works in the 2FA field.
    codes = auth.new_recovery_codes(u["id"])
    auth.audit("user.2fa_enable", "user", u["id"])
    return ok({"enabled": True, "recovery_codes": codes})


@api_bp.route("/2fa/recovery-codes", methods=["POST"])
def twofa_recovery_codes():
    """Replace the recovery codes (e.g. the old 32-bit ones, or after using
    some). Needs a current authenticator code."""
    u = auth.current_user()
    if not u.get("totp_secret"):
        return err("two-factor auth is not enabled")
    if not _current_code_ok(u, _json_body()):
        return err("enter a current authenticator code to regenerate recovery codes")
    codes = auth.new_recovery_codes(u["id"])
    auth.audit("user.2fa_recovery_regenerate", "user", u["id"])
    return ok({"recovery_codes": codes})


@api_bp.route("/2fa/disable", methods=["POST"])
def twofa_disable():
    import totp
    u = auth.current_user()
    if not u.get("totp_secret"):
        return ok({"enabled": False})
    code = _body_str(_json_body(), "code") or ""
    if not totp.verify(auth.totp_secret_of(u) or "", code):
        return err("enter a current authenticator code to disable 2FA")
    auth.store_totp_secret(u["id"], None)
    db.set_recovery_codes(u["id"], None)
    auth.audit("user.2fa_disable", "user", u["id"])
    return ok({"enabled": False})


@api_bp.route("/sessions/revoke", methods=["POST"])
def sessions_revoke():
    from flask import session as _s
    u = auth.current_user()
    _s["epoch"] = db.bump_session_epoch(u["id"])   # keep THIS session signed in
    auth.audit("user.sessions_revoke", "user", u["id"])
    return ok({"revoked": True})


# ---------- user management ----------

@api_bp.route("/users")
def users_list():
    rows = db.list_users()
    return ok([{"id": r["id"], "username": r["username"], "role": r["role"],
                "disabled": bool(r["disabled"]), "last_login_at": r["last_login_at"]}
               for r in rows])


@api_bp.route("/users", methods=["POST"])
def users_create():
    body = _json_body()
    username = body.get("username")
    password = body.get("password")
    # Non-string JSON values used to TypeError into a 500.
    if not isinstance(username, str) or not isinstance(password, str):
        return err("username and password must be strings")
    username = username.strip()
    if not auth.valid_username(username) or len(password) < 8:
        return err("invalid username (1–64 chars: letters, digits, space . _ @ + -) "
                   "or password shorter than 8 chars")
    if db.get_user_by_username(username):
        return err("username already exists", 409)
    role = body.get("role", "user")
    if role not in ("user", "admin"):
        role = "user"
    import sqlite3
    try:
        uid = db.insert_user(username, auth.hash_password(password), role=role)
    except sqlite3.IntegrityError:          # lost a create race on the name
        return err("username already exists", 409)
    auth.audit("user.create", "user", uid, {"username": username})
    return ok({"id": uid})


@api_bp.route("/users/<int:uid>/password", methods=["POST"])
def users_password(uid):
    body = _json_body()
    password = body.get("password")
    if not isinstance(password, str) or len(password) < 8:
        return err("password must be a string of >= 8 chars")
    if not db.get_user(uid):
        return err("no such user", 404)
    db.update_user_password(uid, auth.hash_password(password))
    # An admin reset is incident response: the (possibly stolen) existing
    # sessions must die with the old password.
    new_epoch = db.bump_session_epoch(uid)
    me = auth.current_user()
    if me and me["id"] == uid:
        from flask import session as _s
        _s["epoch"] = new_epoch   # keep the admin's own session alive
    auth.audit("user.password_reset", "user", uid)
    return ok({"id": uid, "sessions_revoked": True})


@api_bp.route("/users/<int:uid>/revoke-sessions", methods=["POST"])
def users_revoke_sessions(uid):
    """Admin kill switch: invalidate every signed-cookie session for a user."""
    if not db.get_user(uid):
        return err("no such user", 404)
    new_epoch = db.bump_session_epoch(uid)
    me = auth.current_user()
    if me and me["id"] == uid:
        from flask import session as _s
        _s["epoch"] = new_epoch
    auth.audit("user.sessions_revoke_admin", "user", uid)
    return ok({"id": uid, "revoked": True})


@api_bp.route("/users/<int:uid>/disable", methods=["POST"])
def users_disable(uid):
    body = _json_body()
    disabled = bool(body.get("disabled", True))
    me = auth.current_user()
    if me and me["id"] == uid and disabled:
        return err("you cannot disable your own account", 400)
    if not db.get_user(uid):
        return err("no such user", 404)
    db.disable_user(uid, disabled)
    auth.audit("user.disable", "user", uid, {"disabled": disabled})
    return ok({"id": uid, "disabled": disabled})


@api_bp.route("/users/<int:uid>", methods=["DELETE"])
def users_delete(uid):
    me = auth.current_user()
    if me and me["id"] == uid:
        return err("you cannot delete your own account", 400)
    if db.count_users() <= 1:
        return err("cannot delete the last remaining user", 400)
    db.delete_user(uid)
    auth.audit("user.delete", "user", uid)
    return ok({"id": uid})


@api_bp.route("/brief/test", methods=["POST"])
def brief_test():
    """Build yesterday's brief and post it to Mattermost (manual trigger).

    Honours a webhook URL / channel posted in the body (like the Test-webhook
    button), so it's consistent with what's typed in the form before saving."""
    import brief
    import notifications
    body = _json_body()
    url = _body_str(body, "MATTERMOST_WEBHOOK_URL")
    _check_webhook(url)
    date = sync.yesterday()
    msg = brief.build_daily_brief(date)
    sent, detail = notifications.post_brief(
        msg, webhook_url=url, channel=_body_str(body, "MATTERMOST_CHANNEL"))
    auth.audit("brief.test", details={"date": date, "sent": sent})
    return ok({"date": date, "sent": sent, "detail": detail, "preview": msg})


@api_bp.route("/brief/status")
def brief_status():
    """Delivery receipt for the daily brief (Settings → Daily brief)."""
    return ok({**sync.brief_status(), "instance": config.instance_name()})


@api_bp.route("/brief/resend", methods=["POST"])
def brief_resend():
    """Re-post a day's brief (default: yesterday) and record the attempt."""
    body = _json_body()
    date = body.get("date") or sync.yesterday()
    if not isinstance(date, str):
        return err("date must be a YYYY-MM-DD string", 400)
    try:
        date = date_cls.fromisoformat(date).isoformat()
    except ValueError:
        return err("invalid date (expected YYYY-MM-DD)", 400)
    if date > sync.today():
        return err("date is in the future", 400)
    sent, detail = sync.resend_brief(date)
    auth.audit("brief.resend", details={"date": date, "sent": sent})
    return ok({"date": date, "sent": sent, "detail": detail})


# ---------- instance identity / primary-standby (O3) ----------

@api_bp.route("/settings/instance")
def settings_instance_get():
    return ok({"instance_name": config.get_text("instance_name"),
               "instance_role": config.instance_role(),
               "peer_url": config.get_text("peer_url")})


@api_bp.route("/settings/instance", methods=["POST"])
def settings_instance_set():
    import re

    import notifications
    body = _json_body()
    vals = {}
    if "instance_name" in body:
        name = body["instance_name"] or ""
        if not isinstance(name, str) or (name and not re.fullmatch(r"[A-Za-z0-9 ._-]{1,40}", name)):
            return err("instance_name: up to 40 letters, digits, space . _ -", 400)
        vals["instance_name"] = name.strip() or None
    if "instance_role" in body:
        role = body["instance_role"]
        if role not in config.ROLES:
            return err("instance_role must be 'primary' or 'standby'", 400)
        vals["instance_role"] = role
    if "peer_url" in body:
        url = (body["peer_url"] or "").strip()
        if url and not notifications.valid_webhook_url(url):
            return err("peer_url must be an http(s) URL such as http://192.168.1.51:8073", 400)
        vals["peer_url"] = url.rstrip("/") or None
    for k, v in vals.items():
        config.set_text(k, v)
    auth.audit("settings.instance", details={k: v for k, v in vals.items()})
    return ok({"saved": True})


@api_bp.route("/backup/latest")
def backup_latest():
    """Download the newest verified nightly backup — a one-click off-box copy.
    (Credentials inside stay Fernet-encrypted: keep the machine_id key seed
    somewhere else, or the copy can't be decrypted.)"""
    from flask import send_file
    path = db.latest_backup()
    if path is None:
        return err("no backup yet — the first is written after the 06:25 sync", 404)
    auth.audit("backup.download", details={"file": path.name})
    return send_file(path, as_attachment=True, download_name=path.name,
                     mimetype="application/vnd.sqlite3", max_age=0)


@api_bp.route("/tariff-sim/run", methods=["POST"])
def tariff_sim_run():
    """Start the tariff comparison (Octopus product data, one request per
    tariff) in the background."""
    import tariff_sim
    body = _json_body()
    try:
        days = int(body.get("days") or 90)
    except (TypeError, ValueError):
        return err("days must be an integer", 400)
    if not 7 <= days <= 366:
        return err("days must be 7–366", 400)
    started = tariff_sim.start_async(days)
    auth.audit("tariff_sim.run", details={"days": days})
    return ok({"started": started})


@api_bp.route("/backfill/status")
def backfill_status():
    lo, hi = db.summary_date_bounds()
    with db.conn() as c:
        n = c.execute("SELECT COUNT(*) FROM daily_summaries").fetchone()[0]
    return ok({**sync.backfill_status(),
               "coverage": {"earliest": lo, "latest": hi, "days_stored": n}})


@api_bp.route("/backfill/start", methods=["POST"])
def backfill_start():
    body = _json_body()
    start = body.get("start") or None
    if start is not None:
        # Validate here — an invalid date reaching the backfill thread used to
        # kill it pre-try and wedge the running flag until a restart.
        if not isinstance(start, str):
            return err("start must be a YYYY-MM-DD string", 400)
        try:
            start = date_cls.fromisoformat(start).isoformat()
        except ValueError:
            return err("invalid start date (expected YYYY-MM-DD)", 400)
    started = sync.start_backfill_async(start=start,
                                        force=bool(body.get("force")))
    auth.audit("backfill.start", details={"force": bool(body.get("force"))})
    return ok({"started": started, **sync.backfill_status()})


@api_bp.route("/backfill/reprice", methods=["POST"])
def backfill_reprice():
    """Re-price stored Octopus history from each half-hour's own tariff
    agreement (Octopus only: one rates request per agreement)."""
    started = sync.start_reprice_async()
    auth.audit("backfill.reprice")
    return ok({"started": started, **sync.backfill_status()})
