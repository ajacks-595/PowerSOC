"""PowerSOC Flask application factory: middleware, security headers, error
handlers and the CLI. Routes live in the `routes/` package (pages, metrics,
api + admin, home); shared helpers in routes/common.py, data builders in
routes/payloads.py.

Run modes:
  python app.py            -> start the web server (port 8073 by default)
  python app.py --migrate  -> initialise/upgrade the database and exit
  python app.py --restore <backup.db> -> replace the DB with a backup (app stopped)
"""
from __future__ import annotations

import logging
from datetime import timedelta

from flask import Flask, render_template, request

import auth
import config
import database as db
import sync
from routes import BLUEPRINTS

# Re-exported for callers/tests that predate the routes/ split.
from routes.common import (  # noqa: F401
    ApiInputError,
    _cache_clear,
    _cached,
    _snap_cache,
    _snap_meta_lock,
    err,
    ok,
)
from routes.payloads import (  # noqa: F401
    _build_day_payload,
    _build_history_payload,
    _build_tips,
)

log = logging.getLogger("powersoc.app")


class TrustedProxyFix:
    """Apply werkzeug's ProxyFix (one hop of X-Forwarded-For/-Proto/-Host)
    ONLY when the socket peer is a configured trusted proxy. A direct-bind
    deploy that is also proxied (LAN :8073 + Cloudron) then sees each internet
    client's real IP and scheme, while a LAN client still can't spoof its IP
    with a header (which blanket TRUST_PROXY would allow)."""

    def __init__(self, wsgi_app, networks):
        from werkzeug.middleware.proxy_fix import ProxyFix
        self.app = wsgi_app
        self.fixed = ProxyFix(wsgi_app, x_for=1, x_proto=1, x_host=1)
        self.networks = tuple(networks)

    def __call__(self, environ, start_response):
        import ipaddress
        try:
            peer = ipaddress.ip_address(environ.get("REMOTE_ADDR", ""))
        except ValueError:
            peer = None
        if peer is not None and any(peer in n for n in self.networks):
            return self.fixed(environ, start_response)
        return self.app(environ, start_response)


def _session_interface():
    from flask.sessions import SecureCookieSessionInterface

    class _Sessions(SecureCookieSessionInterface):
        def get_cookie_secure(self, app):
            # Secure whenever THIS request arrived over HTTPS (directly, or
            # via a trusted proxy's X-Forwarded-Proto) — not only when the
            # operator remembered POWERSOC_COOKIE_SECURE. The plain-HTTP LAN
            # origin keeps a non-Secure cookie (it couldn't send one back).
            return bool(app.config["SESSION_COOKIE_SECURE"] or request.is_secure)
    return _Sessions()


# Sensitive admin responses must never sit in a browser/proxy cache.
_NO_STORE_PREFIXES = ("/api/settings", "/api/users", "/api/2fa", "/api/audit",
                      "/api/sessions", "/api/backup")


# ---------- app factory ---------------------------------------------------

def create_app() -> Flask:
    import os
    config.configure_logging()
    config.ensure_dirs()
    db.init_db()

    # Refuse to run under a different encryption key than the data was saved
    # with: every credential would silently read as unset (2026-10).
    state = config.verify_key_check()
    if state == "mismatch":
        if os.environ.get("POWERSOC_ACCEPT_NEW_KEY") == "1":
            log.warning("POWERSOC_ACCEPT_NEW_KEY=1: adopting the new encryption "
                        "key — stored credentials must be re-entered")
            config.reset_key_check()
        else:
            raise RuntimeError(
                "The credential encryption key has changed (machine id / "
                "POWERSOC_MACHINE_ID(_FILE) / POWERSOC_FERNET_SALT): the stored "
                "credentials can't be decrypted. Restore the original key seed, "
                "or start once with POWERSOC_ACCEPT_NEW_KEY=1 to begin again "
                "with the new key.")
    elif state == "skipped":
        log.error("Stored credentials can't be decrypted with the current "
                  "encryption key — re-enter them in Settings (or restore the "
                  "original key seed)")

    # One pass per version: repair stored days whose battery charge/discharge
    # columns were swapped by the old fixed-sign assumption (2026-10-27).
    try:
        if db.setting_get("battery_orientation_checked") != config.APP_VERSION:
            sync.fix_battery_orientation()
            db.setting_set("battery_orientation_checked", config.APP_VERSION)
    except Exception:  # noqa: BLE001 — never block startup on a data repair
        log.exception("Battery orientation check failed")

    # First-run deep backfill (background thread). No-op if data already exists
    # or no credentials are configured yet.
    try:
        if sync.maybe_start_first_run_backfill():
            log.info("First-run backfill started in background")
    except Exception:  # noqa: BLE001 — never block startup on backfill
        log.exception("Could not start first-run backfill")

    # In-app daily scheduler (Docker deploys, where there's no cron).
    if config.SCHEDULER_ENABLED:
        try:
            if sync.start_scheduler():
                log.info("In-app daily scheduler started")
        except Exception:  # noqa: BLE001
            log.exception("Could not start scheduler")

    try:
        n = auth.migrate_totp_secrets()
        if n:
            log.info("Encrypted %d legacy plaintext TOTP secret(s)", n)
    except Exception:  # noqa: BLE001
        log.exception("TOTP secret migration failed")
    auth.announce_setup_token()
    if not config.key_seed_is_external():
        # Every startup, not just when the seed is first generated: whoever
        # copies the data volume gets the ciphertext AND the key seed.
        # Logs the seed's LOCATION, never its value.
        log.warning("Credential encryption key seed is %s (inside the data "  # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure
                    "directory) — set POWERSOC_MACHINE_ID or "
                    "POWERSOC_MACHINE_ID_FILE from a host-side secret.",
                    config.DATA_DIR / ".instance_id"
                    if config.key_seed_source() == "instance-id"
                    else "the hostname")
    if not config.backup_dir_isolated():
        log.warning("Backups (%s) are on the same disk as the data — a lost "
                    "disk takes them too. Set POWERSOC_BACKUP_MIRROR_DIR or "
                    "copy them off-box.", config.BACKUP_DIR)

    app = Flask(__name__, static_folder="static", template_folder="templates")

    # Versioned static URLs (?v=APP_VERSION) are immutable for a year; anything
    # else under /static/ revalidates. Previously every page load revalidated
    # every asset (Cache-Control: no-cache), incl. 200 KB of Chart.js.
    @app.url_defaults
    def _static_version(endpoint, values):
        if endpoint == "static" and "v" not in values:
            values["v"] = config.APP_VERSION

    _base_max_age = app.get_send_file_max_age

    def _send_file_max_age(filename):
        if request.args.get("v") == config.APP_VERSION:
            return 31536000
        return _base_max_age(filename)
    app.get_send_file_max_age = _send_file_max_age  # type: ignore[method-assign]
    # Behind a trusted reverse proxy, rewrite remote_addr from X-Forwarded-For
    # (one hop) so the login throttle and audit log see the real client IP, not
    # the proxy/gateway. Opt-in (config.TRUST_PROXY) — never trust XFF on a
    # direct-bind deploy, or the throttle could be spoofed. See gotcha in config.
    if config.TRUST_PROXY:
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    elif config.TRUSTED_PROXIES:
        app.wsgi_app = TrustedProxyFix(app.wsgi_app, config.TRUSTED_PROXIES)
    app.secret_key = auth.get_or_create_secret_key()
    app.permanent_session_lifetime = timedelta(days=config.SESSION_DAYS)
    app.session_interface = _session_interface()
    app.config.update(
        # Cookies ignore the port: every other app on this host that uses
        # Flask's default name "session" overwrote ours (random logouts).
        SESSION_COOKIE_NAME="powersoc_session",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=config.COOKIE_SECURE,
        # No route takes uploads; an anonymous 200 MB body to /setup used to
        # be parsed in full (~380 MB RSS on a 4 GB Pi).
        MAX_CONTENT_LENGTH=1024 * 1024,
    )

    import widgets
    app.register_blueprint(auth.auth_bp)
    for bp in BLUEPRINTS:
        app.register_blueprint(bp)
    app.register_blueprint(widgets.widgets_bp)

    @app.before_request
    def _enforce_auth():
        from flask import session as _s
        # CSRF: refuse state-changing requests a browser says did not come
        # from this origin (incl. same-SITE pages on other ports of this host,
        # which SameSite=Lax does not stop). Runs before the public-route
        # early-outs so /login and /setup are covered too.
        reason = auth.cross_origin_write()
        if reason:
            log.warning("Blocked cross-origin %s %r (%s)", request.method,
                        request.path, reason)
            if request.path.startswith("/api/"):
                return err("cross-origin request blocked", 403)
            return "Cross-origin request blocked", 403
        # Static assets, the health probe and widgets never consult the session:
        # merely READING it adds `Vary: Cookie`, which defeats shared caching.
        if (request.endpoint == "static"
                or request.path in ("/api/health", "/sw.js")
                or request.path.startswith(auth.PUBLIC_PREFIXES)):
            return None
        # Only mark the session permanent (which triggers a Set-Cookie) when one
        # actually exists — never on public/no-auth surfaces.
        if _s.get("user_id"):
            _s.permanent = True
        return auth.login_required_globally()

    @app.after_request
    def _security_headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        # NOT no-referrer: over plain HTTP browsers send no Sec-Fetch-Site,
        # and under no-referrer a same-origin <form> POST carries
        # `Origin: null` + no Referer, so the CSRF guard refused every login
        # (2026-10-14). same-origin still sends nothing to other origins.
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault("Permissions-Policy",
                                "geolocation=(), microphone=(), camera=()")
        is_widget = request.path.startswith("/widget/")
        if is_widget:
            # Widgets are meant to be embedded in other dashboards. Allow
            # framing from anywhere and don't send X-Frame-Options.
            resp.headers.setdefault(
                "Content-Security-Policy",
                "default-src 'self'; "
                "script-src 'self' 'unsafe-inline'; "
                "style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data:; "
                "object-src 'none'; "
                "base-uri 'self'")
        else:
            resp.headers.setdefault("X-Frame-Options", "DENY")
            resp.headers.setdefault(
                "Content-Security-Policy",
                "default-src 'self'; "
                "script-src 'self' 'unsafe-inline'; "
                "style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data:; "
                "object-src 'none'; "
                "frame-ancestors 'none'; "
                "form-action 'self'; "
                "base-uri 'self'")
        if config.COOKIE_SECURE:
            resp.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        if request.path == "/settings" or request.path.startswith(_NO_STORE_PREFIXES):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.context_processor
    def _inject_globals():
        # Public, embeddable widgets never touch the session (reading it adds
        # `Vary: Cookie`, which defeats caching in the embedding dashboard).
        if request.path.startswith(auth.PUBLIC_PREFIXES):
            return {"current_user": None, "is_admin": False, "app_name": "PowerSOC"}
        return {"current_user": auth.current_user(),
                "is_admin": auth.is_admin(),
                "app_name": "PowerSOC"}

    @app.errorhandler(ApiInputError)
    def bad_input(e):
        return err(str(e), 400)

    @app.errorhandler(404)
    def not_found(_e):
        if request.path.startswith("/api/"):
            return err("not found", 404)
        return render_template("base.html", theme=config.DEFAULT_THEME,
                               active=None,
                               body="<div class='card'><h1>404</h1>"
                                    "<p>Not found.</p></div>"), 404

    @app.errorhandler(413)
    def too_large(e):
        if request.path.startswith("/api/") or request.is_json:
            return err("request body too large", 413)
        return e

    @app.errorhandler(405)
    def method_not_allowed(e):
        if request.path.startswith("/api/") or request.path == "/metrics":
            return err("method not allowed", 405)
        return e

    @app.errorhandler(Exception)
    def internal_error(e):
        # Defence-in-depth: the {success,data,error} envelope must hold even
        # for bugs we haven't found yet — consumers (main.js, home dashboards,
        # Prometheus) should never see a Werkzeug HTML page.
        from werkzeug.exceptions import HTTPException
        if isinstance(e, HTTPException):
            return e
        log.exception("Unhandled error on %r", request.path)
        if request.path.startswith("/api/") or request.path == "/metrics":
            return err("internal error", 500)
        return render_template("base.html", theme=config.DEFAULT_THEME,
                               active=None,
                               body="<div class='card'><h1>500</h1>"
                                    "<p>Something went wrong.</p></div>"), 500

    return app


if __name__ == "__main__":
    import sys
    if "--restore" in sys.argv:
        # Disaster recovery (run with the app STOPPED):
        #   python app.py --restore /path/to/powersoc-YYYY-MM-DD.db
        config.configure_logging()
        idx = sys.argv.index("--restore")
        if idx + 1 >= len(sys.argv):
            sys.exit("usage: python app.py --restore <backup.db>")
        config.ensure_dirs()
        src = sys.argv[idx + 1]
        moved = db.restore_database(src, accept_new_key="--accept-new-key" in sys.argv)
        print(f"Restored {src} -> {db.DB_PATH}"
              + (f" (previous DB kept at {moved})" if moved else ""), flush=True)
        sys.exit(0)
    if "--migrate" in sys.argv:
        # Optional explicit DB target: `python app.py --migrate /path/to.db`
        config.configure_logging()
        idx = sys.argv.index("--migrate")
        if idx + 1 < len(sys.argv):
            config.DB_PATH = sys.argv[idx + 1]  # type: ignore[misc]
            db.DB_PATH = sys.argv[idx + 1]       # type: ignore[misc]
        config.ensure_dirs()
        db.init_db()
        print(f"Database initialised at {db.DB_PATH}", flush=True)
        sys.exit(0)

    application = create_app()
    application.run(host=config.LISTEN_HOST, port=config.LISTEN_PORT, debug=False)
