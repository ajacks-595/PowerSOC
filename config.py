"""Configuration, environment loading, and crypto setup.

Settings precedence (highest first):
  1. The `settings` table (values set via the /setup wizard or Settings UI),
     Fernet-encrypted at rest.
  2. Environment variables (loaded from a `.env` file in dev, or sourced by the
     systemd unit in prod).
  3. Safe non-secret defaults (port, data dir, theme). Never a credential.

Crypto mirrors the SOC dashboard: a per-machine Fernet key derived from
`/etc/machine-id`, so encrypted credentials are bound to the host and a stolen
DB *file* alone cannot decrypt them.

⚠️ Threat-model caveat (Docker deploy): in a container `/etc/machine-id` is
usually empty, so the key is derived from a random id persisted as
`.instance_id` in DATA_DIR — the SAME volume as `powersoc.db`. So whoever copies
the *volume* gets both ciphertext and key seed: the protection is really
"stolen DB file" not "stolen volume". To bind the key to something outside the
volume, set `POWERSOC_MACHINE_ID` (or `POWERSOC_FERNET_SALT`) from a Docker
secret / host env. NOTE: changing either re-derives the key and invalidates all
already-encrypted credentials, which must then be re-entered — do it
deliberately, not as a drive-by.
"""
from __future__ import annotations

import base64
import logging
import math
import os
import re
import socket
import threading
from dataclasses import dataclass
from hashlib import pbkdf2_hmac
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

log = logging.getLogger("powersoc.config")

# Bumped on each meaningful release; surfaced by /api/health and used to version
# the PWA service-worker cache.
APP_VERSION = "1.6.0"


def _env_int(name: str, default: int) -> int:
    """int(env) with a logged fallback — a malformed operator-set value must
    degrade to the default, not crash the whole app at import time."""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning("Ignoring malformed %s=%r (using default %s)",
                    name, raw, default)
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        log.warning("Ignoring malformed %s=%r (using default %s)",
                    name, raw, default)
        return default


# ---------- .env loading (dev convenience) --------------------------------

def _parse_env_value(raw: str) -> str:
    """Parse one .env value with standard dotenv semantics.

    Quoted values ('…' / "…") are taken verbatim up to the closing quote, so a
    `#` inside quotes survives. For unquoted values, `#` at the start or after
    whitespace begins a comment — `.env.example` documents fields as
    `KEY=   # comment`, and keeping that text made blank fields "configured"
    with the comment (e.g. a POWERSOC_MACHINE_ID taken from a string published
    in the repo, silently voiding at-rest encryption)."""
    v = raw.strip()
    if v[:1] in ('"', "'"):
        end = v.find(v[0], 1)
        return v[1:end] if end != -1 else v[1:]
    if v.startswith("#"):
        return ""
    m = re.search(r"\s#", v)
    return (v[:m.start()] if m else v).strip()


def load_dotenv_file(path: str | Path | None = None) -> None:
    """Populate os.environ from a .env file without overriding existing vars.

    In production the systemd unit sources .env via EnvironmentFile, so this is
    mostly a convenience for `python app.py` during development. No-op if the
    file is absent. Supports `KEY=value` lines, `#` comment lines, inline
    ` # comments` on unquoted values, and quoted values (a `#` inside quotes is
    kept). An EMPTY value means "unset" — it is not exported, so the code
    default applies instead of an empty string (POWERSOC_DB_PATH= used to make
    sqlite open a throwaway temp database).
    """
    p = Path(path) if path else (Path(__file__).resolve().parent / ".env")
    if not p.exists():
        return
    for raw in p.read_text(errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        val = _parse_env_value(val)
        if key and val != "" and key not in os.environ:
            os.environ[key] = val


load_dotenv_file()


# ---------- Crypto (per-machine Fernet) -----------------------------------

_FERNET_SALT_DEFAULT = b"powersoc-v1-deployment-salt"
_FERNET_SALT = (os.environ.get("POWERSOC_FERNET_SALT", "").encode()
                or _FERNET_SALT_DEFAULT)


_MACHINE_ID_PATHS = ("/etc/machine-id", "/var/lib/dbus/machine-id")


def _machine_id() -> str:
    """A stable per-deployment identifier for deriving the Fernet key.

    Order: POWERSOC_MACHINE_ID → POWERSOC_MACHINE_ID_FILE (fail closed) →
    /etc/machine-id → a random id persisted in the data dir. The persisted fallback matters for containers, where
    /etc/machine-id is often empty/absent and the hostname changes on every
    recreate — persisting in the (volume-backed) data dir keeps the key stable
    so encrypted credentials survive restarts and image rebuilds."""
    override = os.environ.get("POWERSOC_MACHINE_ID")
    if override:
        return override
    # Docker-secret style: a file kept OUTSIDE the data volume. Fail CLOSED if
    # it is configured but missing — silently falling back to .instance_id
    # would derive a different key and make every stored credential vanish.
    id_file = os.environ.get("POWERSOC_MACHINE_ID_FILE")
    if id_file:
        try:
            v = Path(id_file).read_text().strip()
        except OSError as e:
            raise RuntimeError(
                f"POWERSOC_MACHINE_ID_FILE={id_file} is not readable ({e}); "
                "refusing to start with a different encryption key") from e
        if not v:
            raise RuntimeError(f"POWERSOC_MACHINE_ID_FILE={id_file} is empty")
        return v
    for p in _MACHINE_ID_PATHS:
        try:
            with open(p) as f:
                v = f.read().strip()
                if v:
                    return v
        except OSError:
            pass
    import secrets as _secrets
    idf = DATA_DIR / ".instance_id"
    try:
        try:
            v = idf.read_text().strip()
            if v:
                return v
        except FileNotFoundError:
            pass
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        v = _secrets.token_hex(16)
        try:
            # 0600 + O_EXCL: this is the key seed for every stored credential,
            # and two processes racing on first start must agree on ONE seed.
            fd = os.open(idf, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            existing = idf.read_text().strip()
            if existing:            # lost the race — use the winner's seed
                return existing
            fd = os.open(idf, os.O_WRONLY | os.O_TRUNC)   # empty file: fill it
        with os.fdopen(fd, "w") as f:
            f.write(v)
        log.warning("Generated a new Fernet key seed in %s — it lives in the "
                    "SAME volume as the encrypted credentials. Set "
                    "POWERSOC_MACHINE_ID(_FILE) from a host-side secret.", idf)
        return v
    except OSError:
        log.error("Cannot read or persist %s — deriving the key from the "
                  "hostname; credentials will not survive a container "
                  "recreate", idf)
        return socket.gethostname() + "-no-machine-id"


def key_seed_source() -> str:
    """Where the Fernet key seed comes from — 'env', 'file', 'machine-id',
    'instance-id' (DATA_DIR/.instance_id: SAME volume as the encrypted DB) or
    'hostname'. Side-effect free (never creates .instance_id)."""
    if os.environ.get("POWERSOC_MACHINE_ID"):
        return "env"
    if os.environ.get("POWERSOC_MACHINE_ID_FILE"):
        return "file"
    for p in _MACHINE_ID_PATHS:
        try:
            with open(p) as f:
                if f.read().strip():
                    return "machine-id"
        except OSError:
            pass
    return "instance-id" if (DATA_DIR / ".instance_id").exists() else "hostname"


def key_seed_is_external() -> bool:
    """True unless the key seed sits next to the data it protects."""
    return key_seed_source() not in ("instance-id", "hostname")


_fernet_cache: dict[tuple[str, bytes], Fernet] = {}
_fernet_lock = threading.Lock()


def fernet() -> Fernet:
    """The per-deployment Fernet instance.

    The PBKDF2(200k) derivation is memoised on its (process-constant) inputs:
    re-deriving on every encrypt/decrypt cost 26 ms per call — 374 ms per
    Settings.load() (14 credentials) and +26 ms on every home-API/metrics
    request. The derived key is byte-identical; only the repetition is gone.
    Keyed on the inputs (not a bare singleton) so a changed machine id/salt
    still takes effect."""
    key = (_machine_id(), _FERNET_SALT)
    f = _fernet_cache.get(key)
    if f is None:
        raw = pbkdf2_hmac("sha256", key[0].encode(), _FERNET_SALT, 200_000, 32)
        f = Fernet(base64.urlsafe_b64encode(raw))
        with _fernet_lock:
            _fernet_cache.clear()
            _fernet_cache[key] = f
    return f


def encrypt(plaintext: str) -> str:
    return fernet().encrypt(plaintext.encode()).decode()


_decrypt_warned = False


def decrypt(ciphertext: str) -> str | None:
    global _decrypt_warned
    try:
        return fernet().decrypt(ciphertext.encode()).decode()
    except (InvalidToken, ValueError):
        # Silent failure here looks like "credentials vanished" to an operator.
        # Warn once per process so a key change (machine-id / salt / moved
        # volume) is diagnosable from the log.
        if not _decrypt_warned:
            _decrypt_warned = True
            log.warning("Stored value failed to decrypt — the Fernet key has "
                        "likely changed (machine-id / POWERSOC_MACHINE_ID / "
                        "POWERSOC_FERNET_SALT). Stored credentials must be "
                        "re-entered.")
        return None


# ---------- key check -------------------------------------------------------
# A value encrypted under the deployment key and stored with the data. If the
# key seed is lost or changed (a redeploy as another user, a missing
# machine_id, a restore elsewhere), it no longer decrypts — and startup and
# restore refuse to proceed instead of silently treating every stored
# credential as unset (2026-10: three weeks of no data, every surface green).
_KEY_CHECK = "key_check"
_KEY_CHECK_PLAINTEXT = "powersoc-key-check-v1"


def key_check_matches(enc: str | None) -> bool:
    if not enc:
        return False
    try:
        return fernet().decrypt(enc.encode()).decode() == _KEY_CHECK_PLAINTEXT
    except (InvalidToken, ValueError):
        return False


def verify_key_check() -> str:
    """'ok' | 'mismatch' | 'created' (first run with this key) | 'skipped'
    (stored credentials exist but none decrypt: never bless a wrong key)."""
    from database import setting_get, setting_keys_with_prefix, setting_set
    enc = setting_get(_KEY_CHECK)
    if enc:
        return "ok" if key_check_matches(enc) else "mismatch"
    stored = [v for v in (setting_get(k) for k in setting_keys_with_prefix(_SETTING_PREFIX)) if v]
    if stored and all(decrypt(v) is None for v in stored):
        return "skipped"
    setting_set(_KEY_CHECK, encrypt(_KEY_CHECK_PLAINTEXT))
    return "created"


def reset_key_check() -> None:
    """Adopt the CURRENT key (a deliberate re-key: POWERSOC_ACCEPT_NEW_KEY=1)."""
    from database import setting_set
    setting_set(_KEY_CHECK, encrypt(_KEY_CHECK_PLAINTEXT))


# ---------- Run mode ------------------------------------------------------

HOSTNAME = socket.gethostname()

_runtime = os.environ.get("POWERSOC_RUNTIME")
if _runtime == "prod":
    IS_PROD = True
elif _runtime == "dev":
    IS_PROD = False
else:
    # Auto-detect: assume prod when running from the install location.
    IS_PROD = Path(__file__).resolve().parent == Path("/opt/powersoc")
IS_DEV = not IS_PROD


# ---------- Paths ---------------------------------------------------------

APP_BASE = (Path("/opt/powersoc") if IS_PROD
            else Path(__file__).resolve().parent)
# `or` (not a .get default): an exported-but-empty variable means "unset".
DATA_DIR = Path(os.environ.get("POWERSOC_DATA_DIR") or str(APP_BASE / "data"))
DB_PATH = os.environ.get("POWERSOC_DB_PATH") or str(DATA_DIR / "powersoc.db")
TOKEN_CACHE_PATH = str(DATA_DIR / "sunsynk_token.json")
# SQLite busy timeout (seconds) for every connection.
DB_TIMEOUT_S = _env_float("POWERSOC_DB_TIMEOUT_S", 30.0)
LOG_PATH = (os.environ.get("POWERSOC_LOG_PATH")
            or ("/var/log/powersoc.log" if IS_PROD else str(APP_BASE / "powersoc.log")))

# Nightly SQLite backups. The default (DATA_DIR/backups) shares the data
# volume's failure domain — on 2026-09-30 the Docker volume was lost and every
# backup went with it. Point this at a different disk / bind mount / synced
# directory; startup warns while it still lives inside DATA_DIR.
BACKUP_DIR = Path(os.environ.get("POWERSOC_BACKUP_DIR") or str(DATA_DIR / "backups"))
BACKUP_KEEP = _env_int("POWERSOC_BACKUP_KEEP", 14)
# Optional second copy of every nightly backup — point it at a DIFFERENT disk
# (a USB drive or NAS mount bind-mounted into the container). On the Pi the
# data volume and the backups dir share one SD card.
BACKUP_MIRROR_DIR = (Path(os.environ["POWERSOC_BACKUP_MIRROR_DIR"])
                     if os.environ.get("POWERSOC_BACKUP_MIRROR_DIR") else None)


def _device_of(path) -> int:
    """st_dev of `path`, or of its nearest existing ancestor."""
    p = Path(path)
    while not p.exists() and p != p.parent:
        p = p.parent
    return os.stat(p).st_dev


def backup_dir_isolated() -> bool:
    """True only when backups land on a different DEVICE from the data.
    Comparing paths called /host/backups vs /data "isolated" on the Pi,
    where both are the same SD card — one card failure takes both."""
    try:
        BACKUP_DIR.resolve().relative_to(DATA_DIR.resolve())
        return False
    except ValueError:
        pass
    try:
        return _device_of(BACKUP_DIR) != _device_of(DATA_DIR)
    except OSError:
        return False


# ---------- Web -----------------------------------------------------------

# Binds all interfaces by design — it's a LAN service (put a reverse proxy in
# front for WAN exposure). Override with POWERSOC_HOST.
LISTEN_HOST = os.environ.get("POWERSOC_HOST", "0.0.0.0")  # noqa: S104  # nosec B104
LISTEN_PORT = _env_int("POWERSOC_PORT", 8073)
COOKIE_SECURE = os.environ.get("POWERSOC_COOKIE_SECURE", "0") == "1"
# Signed-session lifetime in days ("remember me" length). 30 was the historical
# default; tighten for a more cautious posture.
SESSION_DAYS = _env_int("POWERSOC_SESSION_DAYS", 30)

# Trust one layer of X-Forwarded-* (apply werkzeug ProxyFix) so the real client
# IP — not the proxy/gateway IP — drives the login throttle and audit log. Set
# this ONLY when behind a trusted reverse proxy (Traefik/Runtipi, nginx). If the
# app is reachable directly (host/systemd bind on 0.0.0.0), leave it OFF, or a
# client could spoof X-Forwarded-For to dodge the per-IP login throttle.
TRUST_PROXY = os.environ.get("POWERSOC_TRUST_PROXY", "0") == "1"


def parse_proxies(spec: str) -> tuple:
    """'192.168.1.2, 10.0.1.0/24' -> ip_network tuple; malformed entries are
    logged and skipped (never trusted)."""
    import ipaddress
    out = []
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            log.warning("Ignoring malformed POWERSOC_TRUSTED_PROXIES entry %r", part)
    return tuple(out)


# Safer alternative to TRUST_PROXY for a direct-bind deploy that is ALSO
# reached through a reverse proxy (the Pi: LAN on :8073 + Cloudron at
# 192.168.1.2). X-Forwarded-For/-Proto are honoured only when the socket peer
# is one of these addresses — a LAN client can't spoof its IP, while internet
# logins get their real IP (per-client throttle, new-IP alerts, Secure cookie)
# instead of all sharing the proxy's.
TRUSTED_PROXIES = parse_proxies(os.environ.get("POWERSOC_TRUSTED_PROXIES", ""))

# Login brute-force throttle (per client IP). State is persisted in audit_log,
# so it survives restarts and is shared across gunicorn workers. After
# MAX_FAILURES failed logins / setup-token attempts within WINDOW seconds,
# further attempts from that IP are refused until failures age out.
LOGIN_MAX_FAILURES = _env_int("POWERSOC_LOGIN_MAX_FAILURES", 10)
# Ceiling per IP across ALL usernames (password spraying); never reset by a
# successful login, unlike the per-(IP, username) count above.
LOGIN_MAX_FAILURES_PER_IP = _env_int("POWERSOC_LOGIN_MAX_FAILURES_PER_IP", 30)
LOGIN_WINDOW_SECONDS = _env_int("POWERSOC_LOGIN_WINDOW_S", 300)

# Octoplus Saving Sessions via Kraken GraphQL. OFF by default — the documented
# `savingSessions` field drifted on the live API, so enable only once the
# current field name is confirmed (it fails soft either way).
SAVING_SESSIONS_ENABLED = os.environ.get("POWERSOC_SAVING_SESSIONS", "0") == "1"

DEFAULT_THEME = os.environ.get("POWERSOC_DEFAULT_THEME", "dark")
THEMES = ("dark", "midnight", "radar", "terminal", "light")

# In-app scheduler (used by the Docker image, where there is no cron). When
# enabled, app.py runs the daily master sync + brief at 06:25 Europe/London AND
# an intraday refresh of today's data every POWERSOC_INTRADAY_MINUTES. Host/
# systemd installs leave this off and use /etc/cron.d.
SCHEDULER_ENABLED = os.environ.get("POWERSOC_SCHEDULER", "0") == "1"
INTRADAY_MINUTES = _env_int("POWERSOC_INTRADAY_MINUTES", 60)
# Restart the process when a scheduler thread dies or stops ticking (see
# sync.watchdog_reason). POWERSOC_WATCHDOG=0 to disable.
WATCHDOG_ENABLED = os.environ.get("POWERSOC_WATCHDOG", "1") != "0"

# Upstream fetch cadence (server-side cache TTLs, seconds). All tabs/requests
# share these, so they cap Sunsynk call volume regardless of how many clients.
# Live = power flows refreshed ~once a minute; the heavier battery/diagnostics
# detail only ~hourly.
LIVE_CACHE_SECONDS = _env_int("POWERSOC_LIVE_SECONDS", 60)
INDEPTH_CACHE_SECONDS = _env_int("POWERSOC_INDEPTH_SECONDS", 3600)

# Dead-man's switch: a URL pinged (GET) after every successful scheduler tick
# and master run, with "/fail" appended when a run fails (healthchecks.io and
# Uptime Kuma "push" monitors both work). Its ABSENCE is what alerts — an app
# that is down cannot report itself down. Empty = off.
HEARTBEAT_URL = os.environ.get("POWERSOC_HEARTBEAT_URL", "").strip()

# Optional pre-shared first-run setup token. When unset, one is generated and
# logged at startup while no users exist (see auth.setup_token()).
SETUP_TOKEN = os.environ.get("POWERSOC_SETUP_TOKEN", "").strip()

# Intraday Mattermost alerts (deduped once/day). On by default when a webhook
# is configured; disable with POWERSOC_ALERTS=0.
ALERTS_ENABLED = os.environ.get("POWERSOC_ALERTS", "1") != "0"
ALERT_BATTERY_LOW_PCT = _env_float("POWERSOC_ALERT_BATTERY_LOW", 15)
ALERT_HIGH_COST_PENCE = _env_float("POWERSOC_ALERT_HIGH_COST_P", 500)
ALERT_PEAK_KWH = _env_float("POWERSOC_ALERT_PEAK_KWH", 2)
# Optional quiet window for alerts, local hours "22-7" (start-end, end
# exclusive, may wrap midnight). Alerts suppressed during the window are
# re-evaluated (and fire) on the first tick after it ends. Empty = always on.
ALERT_QUIET_HOURS = os.environ.get("POWERSOC_ALERT_QUIET_HOURS", "")

# All energy intervals are reasoned about in UK local time regardless of the
# host clock — Octopus tariff windows are defined in Europe/London.
TIMEZONE = os.environ.get("TZ", "Europe/London")
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()


# ---------- Credentials ---------------------------------------------------

# Credential keys resolvable from the settings table (encrypted) or env vars.
# The wizard writes them to the settings table; env vars are the fallback for
# env-var-style deployments. Listed here so the Settings UI and .env export can
# enumerate them.
CREDENTIAL_KEYS = (
    "SUNSYNK_USERNAME", "SUNSYNK_PASSWORD", "SUNSYNK_INVERTER_SN",
    "SUNSYNK_PLANT_ID",
    "OCTOPUS_API_KEY", "OCTOPUS_ACCOUNT_NUMBER",
    "OCTOPUS_MPAN", "OCTOPUS_METER_SERIAL",
    "OCTOPUS_EXPORT_MPAN", "OCTOPUS_EXPORT_SERIAL",
    "OCTOPUS_MPRN", "OCTOPUS_GAS_SERIAL",
    "MATTERMOST_WEBHOOK_URL", "MATTERMOST_CHANNEL",
)

# Which credential keys hold secrets (masked in the UI / .env export display).
SECRET_KEYS = frozenset({
    "SUNSYNK_PASSWORD", "OCTOPUS_API_KEY", "MATTERMOST_WEBHOOK_URL",
})

_SETTING_PREFIX = "cred:"


def get_credential(name: str, default: str = "") -> str:
    """Resolve a credential: settings table (decrypted) first, then env var.

    Returns "" (or the supplied default) when unset, never None, so callers can
    treat a missing credential as falsy.
    """
    try:
        from database import setting_get
        enc = setting_get(_SETTING_PREFIX + name)
        if enc:
            val = decrypt(enc)
            if val:
                return val
    except Exception:  # noqa: BLE001, S110 — DB may not exist yet on very first run
        pass
    return os.environ.get(name, default)


def unreadable_credentials() -> list[str]:
    """Credentials stored in the settings table that no longer decrypt (the
    Fernet key changed: a restore onto another machine-id, a lost key seed)
    and have no env-var fallback. They read as unset, so a data source just
    stops syncing — 2026-10 lost three weeks that way with nothing on screen.
    Surfaced in Settings, /api/health, the heartbeat and the brief."""
    from database import setting_get
    return [k for k in CREDENTIAL_KEYS
            if (enc := setting_get(_SETTING_PREFIX + k))
            and decrypt(enc) is None and not os.environ.get(k)]


def set_credential(name: str, value: str | None) -> None:
    """Persist a credential to the settings table, Fernet-encrypted.

    Passing None or "" clears it (falls back to env var on next read)."""
    from database import setting_set
    if value:
        setting_set(_SETTING_PREFIX + name, encrypt(value))
    else:
        setting_set(_SETTING_PREFIX + name, None)


def credential_is_set(name: str) -> bool:
    return bool(get_credential(name))


# Non-secret numeric app settings (system cost, battery nameplate, etc.), stored
# plain in the settings table under a cfg: prefix.
def get_number(key: str, default=None):
    try:
        from database import setting_get
        v = setting_get("cfg:" + key)
        if v in (None, ""):
            return default
        f = float(v)
        # A stored 'nan'/'inf' (accepted by older versions) must not leak into
        # maths that ends up as invalid JSON (NaN) or an OverflowError 500.
        return f if math.isfinite(f) else default
    except Exception:  # noqa: BLE001 — bad value or DB not ready
        return default


def parse_finite(value) -> float:
    """float(value), rejecting bools, NaN and ±inf (float() accepts 'nan',
    'inf' and '1e999'). Raises ValueError/TypeError on bad input."""
    if isinstance(value, bool):
        raise TypeError("boolean is not a number")
    f = float(value)
    if not math.isfinite(f):
        raise ValueError("must be a finite number")
    return f


def set_number(key: str, value) -> None:
    """Store a non-secret numeric setting. None/"" clears it; anything else
    must parse as a finite number (raises ValueError/TypeError otherwise —
    previously ANY string was stored and silently ignored on read)."""
    from database import setting_set
    if value in (None, ""):
        setting_set("cfg:" + key, None)
        return
    setting_set("cfg:" + key, repr(parse_finite(value)))


# Non-secret text app settings (gas units, weather location, …), stored plain
# under the same cfg: prefix.
def get_text(key: str, default: str = "") -> str:
    try:
        from database import setting_get
        v = setting_get("cfg:" + key)
        return v if v not in (None, "") else default
    except Exception:  # noqa: BLE001 — DB not ready on very first run
        return default


def set_text(key: str, value: str | None) -> None:
    from database import setting_set
    setting_set("cfg:" + key, value if value else None)


@dataclass(frozen=True)
class Settings:
    """Immutable snapshot of resolved settings for a single operation/request.

    Build with `Settings.load()`. Credentials are resolved at snapshot time so a
    long-running task sees a consistent view; rebuild to pick up wizard changes.
    """
    sunsynk_username: str
    sunsynk_password: str
    sunsynk_inverter_sn: str
    sunsynk_plant_id: str
    octopus_api_key: str
    octopus_account_number: str
    octopus_mpan: str
    octopus_meter_serial: str
    octopus_export_mpan: str
    octopus_export_serial: str
    octopus_mprn: str
    octopus_gas_serial: str
    mattermost_webhook_url: str
    mattermost_channel: str
    # 'm3' (default — converted to kWh at fetch) or 'kwh' for meters that
    # already report kWh; the manual escape hatch for CLAUDE.md gotcha #6.
    gas_units: str = "m3"

    @classmethod
    def load(cls) -> "Settings":
        g = get_credential
        return cls(
            gas_units=(get_text("gas_units",
                                os.environ.get("POWERSOC_GAS_UNITS", "m3"))
                       or "m3").lower(),
            sunsynk_username=g("SUNSYNK_USERNAME"),
            sunsynk_password=g("SUNSYNK_PASSWORD"),
            sunsynk_inverter_sn=g("SUNSYNK_INVERTER_SN"),
            sunsynk_plant_id=g("SUNSYNK_PLANT_ID"),
            octopus_api_key=g("OCTOPUS_API_KEY"),
            octopus_account_number=g("OCTOPUS_ACCOUNT_NUMBER"),
            octopus_mpan=g("OCTOPUS_MPAN"),
            octopus_meter_serial=g("OCTOPUS_METER_SERIAL"),
            octopus_export_mpan=g("OCTOPUS_EXPORT_MPAN"),
            octopus_export_serial=g("OCTOPUS_EXPORT_SERIAL"),
            octopus_mprn=g("OCTOPUS_MPRN"),
            octopus_gas_serial=g("OCTOPUS_GAS_SERIAL"),
            mattermost_webhook_url=g("MATTERMOST_WEBHOOK_URL"),
            # Empty = post to the channel the webhook was created against.
            mattermost_channel=g("MATTERMOST_CHANNEL"),
        )

    @property
    def sunsynk_configured(self) -> bool:
        return bool(self.sunsynk_username and self.sunsynk_password
                    and self.sunsynk_plant_id)

    @property
    def octopus_configured(self) -> bool:
        return bool(self.octopus_api_key and self.octopus_account_number)

    @property
    def mattermost_configured(self) -> bool:
        return bool(self.mattermost_webhook_url)


# ---------- Alerts + heartbeat (Settings first, env fallback) --------------

def alert_config() -> dict:
    """Effective alert settings: Settings → Alerts, else the POWERSOC_ALERT*
    environment defaults above (read at call time so tests can patch them)."""
    enabled = get_text("alerts_enabled")
    return {
        "enabled": ALERTS_ENABLED if enabled == "" else enabled == "1",
        "battery_low_pct": get_number("alert_battery_low_pct", ALERT_BATTERY_LOW_PCT),
        "high_cost_pence": get_number("alert_high_cost_pence", ALERT_HIGH_COST_PENCE),
        "peak_kwh": get_number("alert_peak_kwh", ALERT_PEAK_KWH),
        "quiet_hours": get_text("alert_quiet_hours", ALERT_QUIET_HOURS),
    }


def heartbeat_url() -> str:
    """Dead-man's switch URL: Settings (stored encrypted — it is a capability
    URL) first, then POWERSOC_HEARTBEAT_URL."""
    return (get_credential("HEARTBEAT_URL") or HEARTBEAT_URL or "").strip()


# ---------- Instance identity (O3) ----------------------------------------
# Two copies of PowerSOC ran for months, both posting the brief. Each
# instance has a name (shown in /api/health and on a standby's brief) and a
# role: a "standby" never posts briefs/alerts unless its configured peer is
# unreachable or failed to deliver (see sync._brief_turn).
ROLES = ("primary", "standby")


def instance_name() -> str:
    return (get_text("instance_name") or os.environ.get("POWERSOC_INSTANCE_NAME")
            or HOSTNAME)


def peer_url() -> str:
    """The other instance's base URL (for a standby), Settings first."""
    return (get_text("peer_url") or os.environ.get("POWERSOC_PEER_URL", "")).strip().rstrip("/")


def instance_role() -> str:
    role = (get_text("instance_role") or os.environ.get("POWERSOC_ROLE")
            or "primary").strip().lower()
    return role if role in ROLES else "primary"


# ---------- Helpers -------------------------------------------------------

def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def configure_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
