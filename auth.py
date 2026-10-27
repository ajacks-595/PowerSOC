"""Authentication: PBKDF2-SHA256 passwords, signed session cookies, setup flow.

Mirrors the SOC dashboard scheme:
- First run (empty `users` table) → every route redirects to /setup so the
  operator creates the first admin account.
- Once a user exists, unauthenticated requests redirect to /login (or get a
  401 JSON for /api/* paths).
- `/widget/*` and `/static/*` are public (the widgets are designed to be
  embedded in other dashboards without auth).
- Sessions use Flask's signed-cookie `session`; the secret key is persisted
  Fernet-encrypted in the settings table so cookies survive restarts.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json as _json
import logging
import re
import secrets
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

from flask import Blueprint, g, redirect, render_template, request, session

import config
import database as db

log = logging.getLogger("powersoc.auth")

_PBKDF2_ITERATIONS = 600_000   # OWASP 2023 minimum for PBKDF2-HMAC-SHA256
_PBKDF2_ALGO = "sha256"


def _gen_salt() -> bytes:
    return secrets.token_bytes(16)


def hash_password(password: str, salt: bytes | None = None) -> str:
    """PBKDF2-SHA256. Stored format: `pbkdf2$<iters>$<salt_b64>$<hash_b64>`."""
    salt = salt or _gen_salt()
    h = hashlib.pbkdf2_hmac(_PBKDF2_ALGO, password.encode(), salt, _PBKDF2_ITERATIONS)
    return (f"pbkdf2${_PBKDF2_ITERATIONS}$"
            f"{base64.b64encode(salt).decode()}$"
            f"{base64.b64encode(h).decode()}")


def verify_password(password: str, stored: str) -> bool:
    """Constant-time compare. Returns False on any malformed input."""
    try:
        scheme, iters, salt_b64, hash_b64 = stored.split("$", 3)
        if scheme != "pbkdf2":
            return False
        iters_n = int(iters)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        h = hashlib.pbkdf2_hmac(_PBKDF2_ALGO, password.encode(), salt, iters_n)
        return hmac.compare_digest(h, expected)
    except (ValueError, TypeError):
        return False


def needs_rehash(stored: str) -> bool:
    """True when a stored hash uses fewer PBKDF2 iterations than we now mint (or
    is malformed/legacy), so it should be re-hashed on the next login."""
    try:
        scheme, iters, _salt, _hash = stored.split("$", 3)
        return scheme != "pbkdf2" or int(iters) < _PBKDF2_ITERATIONS
    except (ValueError, TypeError):
        return True


# Usernames: letters, digits, space and . _ @ + - ; 1–64 chars. Keeps stored
# names safe to render and rejects HTML/control characters (defence-in-depth
# alongside output escaping).
_USERNAME_RE = re.compile(r"^[A-Za-z0-9 ._@+\-]{1,64}$")


def valid_username(name: str) -> bool:
    return bool(_USERNAME_RE.match(name or ""))


def safe_next_path(target: str | None) -> str:
    """Return `target` if it is a safe same-origin relative path, else '/'.

    Prevents open redirects. The naive `startswith('/')` check is NOT enough:
    browsers normalise backslashes to '/', so '/\\evil.com' becomes the
    protocol-relative '//evil.com'. Reject anything with a scheme, a netloc,
    a backslash, a control char, or a leading '//'."""
    from urllib.parse import urlsplit
    if (not target or "\\" in target
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in target)):
        return "/"
    if not target.startswith("/") or target.startswith("//"):
        return "/"
    parts = urlsplit(target)
    if parts.scheme or parts.netloc:
        return "/"
    return target


# ---------- login brute-force throttle (per client IP, persisted) ---------
# A dummy hash so the no-such-user path performs the same PBKDF2 work as a real
# verify, removing a username-enumeration timing oracle.
_DUMMY_HASH = hash_password(secrets.token_hex(16))
# Throttle state lives in audit_log (every failure is already recorded there
# with its IP) instead of a per-process dict, so it survives restarts and is
# shared by every worker. Timestamps are written by the app from time.time()
# — not SQLite's CURRENT_TIMESTAMP — so writer and reader use ONE clock.
_THROTTLED_DETAILS = _json.dumps({"reason": "throttled"})


def _throttle_key() -> str:
    return request.remote_addr or "?"


def _utc_stamp(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def login_blocked(username: str = "") -> float:
    """Seconds remaining in a lockout for this attempt, or 0.0 if allowed.

    Two counts over the window, from audit_log (attempts rejected *because*
    of a lockout aren't counted, so a block still expires on schedule):
    - this IP + this username, since that username's last success from this
      IP — a typo'd password clears on the next good login;
    - this IP across all usernames, never reset (password spraying).
    It used to be one per-IP count reset by ANY success from the IP, so an
    attacker with their own account there (or behind the same proxy) could
    reset it at will, and 10 failures locked out everyone behind a proxy."""
    now = time.time()
    window = config.LOGIN_WINDOW_SECONDS
    ip = _throttle_key()
    username = (username or "")[:64]
    with db.conn() as c:
        rows = c.execute(
            """SELECT id, username, created_at FROM audit_log
               WHERE action='user.login_failed' AND user_id IS NULL
                 AND ip_address=? AND created_at>=?
                 AND (details IS NULL OR details!=?)
               ORDER BY created_at""",
            (ip, _utc_stamp(now - window), _THROTTLED_DETAILS)).fetchall()
        last_ok = c.execute(
            """SELECT MAX(id) FROM audit_log
               WHERE action IN ('user.login', 'user.setup')
                 AND ip_address=? AND COALESCE(username, '')=?""",
            (ip, username)).fetchone()[0] or 0
    mine = [r for r in rows if (r["username"] or "") == username and r["id"] > last_ok]

    def remaining(rs, limit) -> float:
        if len(rs) < limit:
            return 0.0
        # Blocked until enough failures age out to drop below the limit.
        pivot = datetime.strptime(rs[len(rs) - limit]["created_at"], "%Y-%m-%d %H:%M:%S")
        pivot_ts = pivot.replace(tzinfo=timezone.utc).timestamp()
        return max(1.0, round(window - (now - pivot_ts), 1))

    return max(remaining(mine, config.LOGIN_MAX_FAILURES),
               remaining(rows, config.LOGIN_MAX_FAILURES_PER_IP))


# ---------- cross-origin (CSRF) guard -------------------------------------
# SameSite=Lax stops CROSS-SITE requests carrying the session, but "site"
# ignores the port: every other app on this host (other self-hosted apps,
# a dashboard on :8090, …) is SAME-SITE, so a form/text-plain POST from
# any of them arrived WITH the admin's cookie and was acted on (verified
# 2026-10-07: start backfill, disable a user, regenerate the home token).
# Browsers label every request's provenance; reject state-changing ones that
# are not same-origin. Non-browser clients send none of these headers — and
# hold no ambient cookie to abuse — so they are unaffected.
# Sec-Fetch-Site is only sent to secure origins (HTTPS / localhost); on the
# plain-HTTP LAN deploy the Origin fallback is what runs, and it depends on
# our Referrer-Policy not being no-referrer (see app._security_headers).
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _netloc(url_or_host: str) -> str:
    n = url_or_host.lower()
    for default in (":80", ":443"):
        if n.endswith(default):
            n = n[: -len(default)]
    return n


def cross_origin_write() -> str | None:
    """Why this request must be refused as a cross-origin write, or None."""
    if request.method in _SAFE_METHODS:
        return None
    sfs = request.headers.get("Sec-Fetch-Site")
    if sfs is not None:                       # every current browser sends it
        # Reasons are for the server LOG only (never sent to the client);
        # header values are repr()'d and truncated so they can't forge lines.
        return (None if sfs in ("same-origin", "none")
                else f"Sec-Fetch-Site={sfs[:40]!r}")  # nosemgrep: python.flask.security.audit.directly-returned-format-string.directly-returned-format-string
    host = _netloc(request.host)
    origin = request.headers.get("Origin")
    if origin is not None:
        if origin == "null" or _netloc(urlsplit(origin).netloc) != host:
            return f"Origin={origin[:100]!r}"  # nosemgrep: python.flask.security.audit.directly-returned-format-string.directly-returned-format-string
        return None
    referer = request.headers.get("Referer")
    if referer and _netloc(urlsplit(referer).netloc) != host:
        return "Referer is cross-origin"
    return None


# ---------- Flask integration --------------------------------------------

PUBLIC_ENDPOINTS = {
    "auth.login_page", "auth.login_submit",
    "auth.setup_page", "auth.setup_submit",
    "auth.logout",
    "api.health",            # public, minimal liveness probe (no secrets)
    "pages.service_worker",  # /sw.js must load without a session
    "static",
}

# Path prefixes that never require a session (embeddable widgets).
PUBLIC_PREFIXES = ("/widget/",)

# Cloudron's App Proxy reserves /login and /logout on the app's own domain for
# its sign-in wall, so a redirect to PowerSOC's /login there lands on
# Cloudron's page again — a login loop. The canonical paths live under /auth/;
# /login and /logout remain as aliases for LAN bookmarks.
LOGIN_PATH = "/auth/login"
LOGOUT_PATH = "/auth/logout"


def current_user() -> dict | None:
    """Return the logged-in user dict, or None. Cached per-request on g."""
    cached = getattr(g, "_current_user", None)
    if cached is not None:
        return cached or None
    user_id = session.get("user_id")
    if not user_id:
        g._current_user = False
        return None
    row = db.get_user(user_id)
    if not row or row["disabled"]:
        session.pop("user_id", None)
        g._current_user = False
        return None
    # Session epoch: a pre-existing session with no epoch adopts the current one
    # (stays valid); a mismatch means the user revoked sessions -> log this out.
    epoch = row["session_epoch"]
    if "epoch" not in session:
        session["epoch"] = epoch
    elif session["epoch"] != epoch:
        session.clear()
        g._current_user = False
        return None
    user = dict(row)
    g._current_user = user
    return user


def is_admin() -> bool:
    """True when the current session belongs to an admin-role user."""
    user = current_user()
    return bool(user and user.get("role") == "admin")


def _json_401(error: str, redirect_to: str):
    from flask import jsonify
    return jsonify({"success": False, "data": None, "error": error,
                    "redirect": redirect_to}), 401


def _json_403(error: str):
    from flask import jsonify
    return jsonify({"success": False, "data": None, "error": error}), 403


# Endpoints reserved for admins: all of Settings + Users management, plus the
# data-mutating brief test and backfill trigger. Read-only data endpoints (and
# /api/backfill/status) stay open to any signed-in user. Path-based so any new
# /api/settings or /api/users sub-route is admin-by-default (fail-safe).
def _is_admin_only(path: str) -> bool:
    if path == "/settings":
        return True
    if path.startswith(("/api/settings", "/api/users", "/api/backup", "/api/audit")):
        return True
    return path in ("/api/brief/test", "/api/brief/resend", "/api/backfill/start",
                    "/api/backfill/reprice", "/api/tariff-sim/run")


def login_required_globally():
    """Before-request middleware: gate everything except public surfaces."""
    if request.endpoint in PUBLIC_ENDPOINTS:
        return None
    if request.endpoint and request.endpoint.startswith("static"):
        return None
    if any(request.path.startswith(p) for p in PUBLIC_PREFIXES):
        return None

    # Home-consumer API (home dashboards, Home Assistant) + Prometheus /metrics: token-gated,
    # read-only, NOT session-gated. Default-OFF until a token is set in Settings.
    if request.path.startswith("/api/home/") or request.path == "/metrics":
        return _enforce_home_token()

    # First run: no users yet → force the setup flow.
    if db.count_users() == 0:
        if request.endpoint == "auth.setup_page":
            return None
        if request.path.startswith("/api/"):
            return _json_401("setup required", "/setup")
        return redirect("/setup")

    user = current_user()
    if user:
        # Authenticated: admins go anywhere; non-admins are read-only and may
        # not reach Settings / Users management or the mutating triggers.
        if _is_admin_only(request.path) and user.get("role") != "admin":
            if request.path.startswith("/api/"):
                return _json_403("admin privileges required")
            return redirect("/")
        return None

    if request.path.startswith("/api/"):
        return _json_401("authentication required", LOGIN_PATH)
    # Percent-encoded: a path with a newline made the Location header invalid
    # (a 500 for anyone, and a forged line in the log).
    from urllib.parse import quote
    return redirect(LOGIN_PATH + "?next=" + quote(request.path, safe="/"))


def audit(action: str, target_type: str | None = None,
          target_id: str | int | None = None, details: dict | None = None) -> None:
    """Add an audit entry stamped with the current user and request IP."""
    user = current_user() or {}
    db.audit_add(
        user_id=user.get("id"),
        username=user.get("username"),
        action=action,
        target_type=target_type,
        target_id=str(target_id) if target_id is not None else None,
        details=_json.dumps(details) if details else None,
        ip_address=request.remote_addr if request else None,
    )


# ---------- home-consumer API token --------------------------------------

# A shared bearer token gates /api/home/* (read-only) for trusted LAN consumers
# like a home dashboard. Stored Fernet-encrypted; default-OFF (no token = 403).
_HOME_TOKEN_KEY = "home_api_token"  # noqa: S105 - settings key name, not a secret


def home_api_token_get() -> str | None:
    enc = db.setting_get(_HOME_TOKEN_KEY)
    return config.decrypt(enc) if enc else None


def home_api_token_set(token: str | None) -> None:
    db.setting_set(_HOME_TOKEN_KEY, config.encrypt(token) if token else None)


def home_api_generate_token() -> str:
    token = secrets.token_urlsafe(32)
    home_api_token_set(token)
    return token


def home_api_enabled() -> bool:
    return bool(home_api_token_get())


def _present_home_token() -> str | None:
    # Header only — deliberately NOT accepting ?token= so the token never ends
    # up in access logs / browser history.
    hdr = request.headers.get("X-PowerSOC-Token")
    if hdr:
        return hdr
    authz = request.headers.get("Authorization", "")
    if authz.startswith("Bearer "):
        return authz[len("Bearer "):].strip()
    return None


def _enforce_home_token():
    """Per-consumer tokens (home_tokens.py) + the legacy shared token."""
    from flask import jsonify

    import home_tokens
    if not home_tokens.any_configured():
        return jsonify({"success": False, "data": None,
                        "error": "home API disabled — set a token in Settings"}), 403
    presented = _present_home_token()
    consumer = home_tokens.identify(presented)
    if consumer is None:
        # Counted by fingerprint and logged at most hourly — never the token.
        home_tokens.record_reject(presented, request.remote_addr, request.path)
        return jsonify({"success": False, "data": None,
                        "error": "invalid or missing home API token"}), 401
    home_tokens.record_use(consumer, request.remote_addr)
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        return jsonify({"success": False, "data": None,
                        "error": "home API is read-only"}), 403
    return None


# ---------- TOTP secrets at rest ------------------------------------------
# Fernet-encrypted like every other credential (they were plaintext, so a
# stolen DB file yielded working 2FA seeds). Base32 secrets are uppercase and
# never start with Fernet's "gAAAA" prefix, so legacy rows are detectable.

def _is_encrypted(v: str | None) -> bool:
    return bool(v) and v.startswith("gAAAA")


def totp_secret_of(row) -> str | None:
    """Decrypted TOTP secret for a user row (None if 2FA off or undecryptable
    — fail closed: TOTP then can't verify, recovery codes still work)."""
    raw = row["totp_secret"] if row else None
    if not raw:
        return None
    if not _is_encrypted(raw):
        return raw                      # legacy plaintext (pre-migration)
    secret = config.decrypt(raw)
    if secret is None:
        # Logs the user id only, never secret material.
        log.error("TOTP secret for user id=%s cannot be decrypted (key "  # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure
                  "changed?) — use a recovery code", row["id"])
    return secret


def store_totp_secret(user_id: int, secret: str | None) -> None:
    db.set_user_totp(user_id, config.encrypt(secret) if secret else None)


def migrate_totp_secrets() -> int:
    """Encrypt any legacy plaintext TOTP secrets in place (idempotent)."""
    n = 0
    for row in db.list_users():
        raw = row["totp_secret"]
        if raw and not _is_encrypted(raw):
            db.set_user_totp_raw(row["id"], config.encrypt(raw))
            n += 1
    return n


# ---------- first-run setup token ----------------------------------------
# With an empty users table, whoever reached /setup first became admin — on a
# LAN that is anyone, during exactly the window a fresh (re)deploy is up. The
# token is printed to the server log (and can be pre-set via
# POWERSOC_SETUP_TOKEN), so only someone with access to the host can claim it.
_SETUP_TOKEN_KEY = "setup_token"  # noqa: S105 - settings key name, not a secret


def setup_token() -> str | None:
    """The one-time setup token, or None once any user exists."""
    if db.count_users() > 0:
        return None
    if config.SETUP_TOKEN:
        return config.SETUP_TOKEN
    tok = db.setting_get(_SETUP_TOKEN_KEY)
    if not tok:
        # Insert-if-absent, then re-read: concurrent first starts agree.
        db.setting_insert_if_absent(
            _SETUP_TOKEN_KEY, "-".join(secrets.token_hex(3) for _ in range(4)))
        tok = db.setting_get(_SETUP_TOKEN_KEY)
    return tok


def announce_setup_token() -> None:
    tok = setup_token()
    if tok:
        log.warning("First-run setup: open /setup and enter the one-time setup "
                    "token: %s", tok)


def _setup_token_ok(presented) -> bool:
    expected = setup_token()
    if not expected or not isinstance(presented, str) or not presented.isascii():
        return False
    return hmac.compare_digest(presented.strip().encode(), expected.encode())


# ---------- blueprint -----------------------------------------------------

auth_bp = Blueprint("auth", __name__)


@auth_bp.route("/setup", methods=["GET"])
def setup_page():
    # Phase 1: minimal admin-creation step. The full multi-step wizard
    # (Sunsynk / Octopus / notifications / backfill) is layered on in Phase 8.
    if db.count_users() > 0:
        return redirect(LOGIN_PATH)
    setup_token()   # make sure one exists (and was logged) before the form
    return render_template("setup.html", theme=config.DEFAULT_THEME)


@auth_bp.route("/setup", methods=["POST"])
def setup_submit():
    """Create the first admin account and log in. Supports both a JSON request
    (the wizard advances client-side) and a plain form post (no-JS fallback)."""
    from flask import jsonify
    wants_json = (request.is_json
                  or "application/json" in request.headers.get("Accept", ""))
    # Checked BEFORE the body is parsed: a finished install must not spend
    # memory parsing an anonymous request's body (a 200 MB one cost ~380 MB).
    if db.count_users() > 0:
        if wants_json:
            return jsonify({"success": False, "data": None,
                            "error": "setup already complete"}), 409
        return redirect(LOGIN_PATH)
    data = request.get_json(silent=True)
    if data is None:
        data = request.form
    if not hasattr(data, "get"):     # a JSON array/string body used to 500
        return jsonify({"success": False, "data": None,
                        "error": "JSON body must be an object"}), 400

    blocked = login_blocked("")
    if blocked or not _setup_token_ok(data.get("setup_token")):
        _audit_login_failure("", "throttled" if blocked else "bad_setup_token")
        msg = (f"Too many attempts — try again in {int(blocked) + 1}s." if blocked
               else "Invalid setup token — it is printed in the server log "
                    "(look for 'setup token').")
        if wants_json:
            return jsonify({"success": False, "data": None, "error": msg}), \
                (429 if blocked else 403)
        return render_template("setup.html", theme=config.DEFAULT_THEME,
                               error=msg), (429 if blocked else 403)

    username = data.get("username") or ""
    password = data.get("password") or ""
    if not isinstance(username, str) or not isinstance(password, str):
        username, password = "", ""
    username = username.strip()
    if not valid_username(username) or len(password) < 8:
        msg = ("Username must be 1–64 chars (letters, digits, space, . _ @ + -) "
               "and password at least 8 characters.")
        if wants_json:
            return jsonify({"success": False, "data": None, "error": msg}), 400
        return render_template("setup.html", theme=config.DEFAULT_THEME,
                               error=msg), 400

    import sqlite3
    try:
        # Atomic first-admin insert: closes the check-then-insert race where
        # two concurrent setups (same or different usernames) both pass the
        # count_users()==0 guard above.
        uid = db.insert_first_admin(username, hash_password(password))
    except sqlite3.IntegrityError:
        uid = None
    if uid is None:
        if wants_json:
            return jsonify({"success": False, "data": None,
                            "error": "setup already complete"}), 409
        return redirect(LOGIN_PATH)
    db.setting_delete(_SETUP_TOKEN_KEY)   # one-time: never valid again
    session["user_id"] = uid
    # Epoch must be set on EVERY authenticated session — one minted without it
    # would silently adopt the next epoch and survive a "revoke all sessions".
    session["epoch"] = 0
    session.permanent = True
    db.update_user_login(uid)
    audit("user.setup", "user", uid, {"username": username})
    if wants_json:
        return jsonify({"success": True, "data": {"id": uid}, "error": None})
    # No-JS fallback: land on Settings to finish configuring.
    return redirect("/settings?setup=1")


@auth_bp.route("/login", methods=["GET"])       # alias (LAN bookmarks)
@auth_bp.route(LOGIN_PATH, methods=["GET"])
def login_page():
    next_path = safe_next_path(request.args.get("next", "/"))
    return render_template("login.html", theme=config.DEFAULT_THEME, next=next_path)


def _audit_login_failure(username: str, reason: str) -> None:
    """Persist failed-auth evidence — and the login throttle's state (see
    login_blocked), stamped from the same clock it is read with.

    Bounded: usernames are cut to the 64-char maximum, and attempts refused
    BY the lockout are recorded at most once a minute per IP — previously
    every one wrote a row with the full submitted name (90 MB in 3.5 s from
    one LAN client)."""
    now = time.time()
    ip = request.remote_addr if request else None
    if reason == "throttled":
        with db.conn() as c:
            recent = c.execute(
                "SELECT 1 FROM audit_log WHERE action='user.login_failed' "
                "AND ip_address=? AND details=? AND created_at>=? LIMIT 1",
                (ip, _THROTTLED_DETAILS, _utc_stamp(now - 60))).fetchone()
        if recent:
            return
    db.audit_add(user_id=None, username=(username or "")[:64] or None,
                 action="user.login_failed", target_type="user",
                 details=_json.dumps({"reason": reason}),
                 ip_address=ip, created_at=_utc_stamp(now))


def _recovery_hash(code: str) -> str:
    norm = "".join(code.split()).replace("-", "").lower()
    return hashlib.sha256(norm.encode()).hexdigest()


def new_recovery_codes(user_id: int) -> list[str]:
    """Mint 10 one-time recovery codes (80 random bits each, shown ONCE,
    formatted XXXX-XXXX-XXXX-XXXX) and store their hashes, replacing any
    previous set. The old 8-hex-char codes were 32 bits; at 80 bits an
    unsalted SHA-256 is no longer brute-forceable from a stolen DB."""
    codes = []
    for _ in range(10):
        raw = base64.b32encode(secrets.token_bytes(10)).decode()   # 16 chars
        codes.append("-".join(raw[i:i + 4] for i in range(0, 16, 4)))
    db.set_recovery_codes(user_id, _json.dumps([_recovery_hash(c) for c in codes]))
    return codes


def _consume_recovery_code(row, code: str) -> bool:
    """Try `code` as a one-time recovery code; consume it on match.
    Compare-and-swap on the stored list, so two concurrent logins holding
    the same code can't both succeed."""
    raw = row["recovery_codes"]
    if not raw or not code or not code.isascii():
        return False
    for _ in range(5):          # retry if ANOTHER code was consumed meanwhile
        try:
            hashes = _json.loads(raw)
        except (ValueError, TypeError):
            return False
        h = _recovery_hash(code)
        if h not in hashes:
            return False
        hashes.remove(h)
        if db.swap_recovery_codes(row["id"], raw, _json.dumps(hashes)):
            audit("user.recovery_code_used", "user", row["id"],
                  {"remaining": len(hashes)})
            return True
        fresh = db.get_user(row["id"])
        raw = fresh["recovery_codes"] if fresh else None
        if not raw:
            return False
    return False


def _second_factor_ok(row, presented: str) -> bool:
    """TOTP with single-use (replay) enforcement, or a one-time recovery code."""
    import totp
    counter = totp.verify_counter(totp_secret_of(row) or "", presented)
    if counter is not None:
        if not db.claim_totp_counter(row["id"], counter):
            log.warning("TOTP replay rejected for user id=%s", row["id"])
            return False
        return True
    return _consume_recovery_code(row, presented)


def _notify_new_ip(row) -> None:
    """Best-effort Mattermost note when an account signs in from an IP it has
    never used before (cheap, high-signal for a self-hosted box)."""
    ip = request.remote_addr if request else None
    if not ip:
        return
    with db.conn() as c:
        seen = c.execute(
            "SELECT 1 FROM audit_log WHERE action='user.login' AND user_id=? "
            "AND ip_address=? LIMIT 1", (row["id"], ip)).fetchone()
        ever = c.execute(
            "SELECT 1 FROM audit_log WHERE action='user.login' AND user_id=? "
            "LIMIT 1", (row["id"],)).fetchone()
    if seen or not ever:    # known IP, or first-ever login (nothing to compare)
        return
    text = (f"### 🔑 PowerSOC login from a new IP\n"
            f"User **{row['username']}** signed in from **{ip}**.")

    def _send():
        try:
            import notifications
            notifications.post_brief(text)
        except Exception:  # noqa: BLE001 — best-effort
            log.exception("new-IP notification failed")
    # Background thread: post_brief retries with backoff (up to ~a minute on a
    # dead webhook), which used to stall the login response that long.
    threading.Thread(target=_send, name="powersoc-newip", daemon=True).start()


@auth_bp.route("/login", methods=["POST"])       # alias (LAN bookmarks)
@auth_bp.route(LOGIN_PATH, methods=["POST"])
def login_submit():
    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    next_path = request.form.get("next") or "/"
    blocked = login_blocked(username)
    if blocked:
        _audit_login_failure(username, "throttled")
        return render_template(
            "login.html", theme=config.DEFAULT_THEME,
            error=f"Too many failed attempts — try again in {int(blocked) + 1}s.",
            next=next_path), 429
    row = db.get_user_by_username(username)
    # Always run a PBKDF2 verify (against a dummy hash when the user is unknown)
    # so response time doesn't reveal whether the username exists.
    stored = row["password_hash"] if row else _DUMMY_HASH
    password_ok = verify_password(password, stored)
    if row and not row["disabled"] and password_ok:
        # Second factor, if the account has TOTP enabled.
        if row["totp_secret"]:
            if not _second_factor_ok(row, request.form.get("totp") or ""):
                _audit_login_failure(username, "bad_second_factor")
                # Same message as a wrong password: a distinct error would
                # confirm the password is correct (a credential oracle that
                # undermines the second factor).
                return render_template(
                    "login.html", theme=config.DEFAULT_THEME,
                    error="Invalid username, password, or code.",
                    next=next_path, show_totp=True), 401
        _notify_new_ip(row)
        session["user_id"] = row["id"]
        session["epoch"] = row["session_epoch"]
        session.permanent = True
        db.update_user_login(row["id"])
        audit("user.login", "user", row["id"])
        # Opportunistically upgrade an old (lower-iteration) hash now that the
        # login has succeeded and we hold the plaintext.
        if needs_rehash(row["password_hash"]):
            db.update_user_password(row["id"], hash_password(password))
        # Prevent open redirect: only same-origin relative paths.
        return redirect(safe_next_path(next_path))
    _audit_login_failure(username, "bad_credentials")
    return render_template(
        "login.html", theme=config.DEFAULT_THEME,
        error="Invalid username, password, or code.", next=next_path), 401


@auth_bp.route("/logout")                       # alias
@auth_bp.route(LOGOUT_PATH)
def logout():
    user = current_user()
    if user:
        audit("user.logout", "user", user["id"])
    # Clear EVERYTHING — leaving epoch/pending_totp behind interacts badly
    # with the epoch-adoption logic and mid-enrolment state.
    session.clear()
    return redirect(LOGIN_PATH)


# ---------- secret key bootstrap ------------------------------------------

def get_or_create_secret_key() -> bytes:
    """Persist Flask's session secret (Fernet-encrypted) so cookies survive
    restarts. Generated once on first run."""
    enc = db.setting_get("flask_secret_key")
    if enc:
        sk = config.decrypt(enc)
        if sk:
            return base64.b64decode(sk)
    raw = secrets.token_bytes(32)
    db.setting_set("flask_secret_key",
                   config.encrypt(base64.b64encode(raw).decode()))
    return raw
