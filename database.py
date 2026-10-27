"""SQLite schema + CRUD helpers for PowerSOC.

Conventions (shared with the SOC dashboard):
- `SCHEMA` is applied with `executescript`; every table uses
  `CREATE TABLE IF NOT EXISTS` so init is idempotent.
- `conn()` yields an autocommit (`isolation_level=None`) connection in WAL mode
  with `row_factory=sqlite3.Row` and foreign keys on.
- Column additions go in `_MIGRATIONS` as `(table, column, ddl)` tuples and are
  applied only when missing (checked via `PRAGMA table_info`).
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import date as _date
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

from config import DB_PATH, DB_TIMEOUT_S

SCHEMA = """
-- Half-hourly electricity import from Octopus
CREATE TABLE IF NOT EXISTS electricity_intervals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    interval_start  TEXT NOT NULL,     -- ISO 8601, Europe/London
    interval_end    TEXT NOT NULL,
    consumption_kwh REAL NOT NULL,
    tariff_window   TEXT NOT NULL,     -- 'cheap' | 'day' | 'peak'
    rate_p_per_kwh  REAL,              -- p/kWh inc VAT at time of interval
    cost_pence      REAL,              -- consumption_kwh * rate_p_per_kwh
    UNIQUE(interval_start)
);

-- Half-hourly export via the Outgoing Octopus MPAN
CREATE TABLE IF NOT EXISTS export_intervals (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    interval_start TEXT NOT NULL,
    interval_end   TEXT NOT NULL,
    export_kwh     REAL NOT NULL,
    rate_p_per_kwh REAL,              -- SEG/export rate at time of interval
    earned_pence   REAL,
    UNIQUE(interval_start)
);

-- Half-hourly gas consumption (stored in kWh after m3 conversion)
CREATE TABLE IF NOT EXISTS gas_intervals (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    interval_start TEXT NOT NULL,
    interval_end   TEXT NOT NULL,
    consumption_kwh REAL NOT NULL,
    rate_p_per_kwh REAL,
    cost_pence     REAL,
    UNIQUE(interval_start)
);

-- Half-hourly Sunsynk energy-flow data (derived from /day series)
CREATE TABLE IF NOT EXISTS solar_intervals (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    interval_start        TEXT NOT NULL,
    pv_kwh                REAL,
    battery_charge_kwh    REAL,
    battery_discharge_kwh REAL,
    grid_import_kwh       REAL,
    grid_export_kwh       REAL,
    house_load_kwh        REAL,
    soc_pct               REAL,
    UNIQUE(interval_start)
);

-- Pre-aggregated daily totals (written after a day's data is fully collected)
CREATE TABLE IF NOT EXISTS daily_summaries (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    date                  TEXT NOT NULL UNIQUE,   -- YYYY-MM-DD
    -- Solar
    pv_kwh                REAL,
    battery_charge_kwh    REAL,
    battery_discharge_kwh REAL,
    peak_soc_pct          REAL,
    end_soc_pct           REAL,
    -- Electricity
    grid_import_kwh       REAL,
    grid_export_kwh       REAL,
    house_load_kwh        REAL,
    cheap_import_kwh      REAL,
    day_import_kwh        REAL,
    peak_import_kwh       REAL,
    import_cost_pence     REAL,
    export_earned_pence   REAL,
    standing_elec_pence   REAL,
    -- Gas
    gas_kwh               REAL,
    gas_cost_pence        REAL,
    standing_gas_pence    REAL,
    -- Derived
    self_sufficiency_pct  REAL,
    net_cost_pence        REAL,
    sunsynk_ok            INTEGER,
    octopus_ok            INTEGER,
    data_partial          INTEGER,
    export_estimated      INTEGER,  -- 1 = grid_export is a Sunsynk estimate
    import_estimated      INTEGER   -- 1 = grid_import is a Sunsynk CT estimate
);

-- Cached rate history (refreshed weekly)
CREATE TABLE IF NOT EXISTS tariff_rates (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    fuel               TEXT NOT NULL,  -- 'electricity_import' | 'electricity_export' | 'gas'
    valid_from         TEXT NOT NULL,
    valid_to           TEXT,
    rate_p_per_kwh     REAL NOT NULL,
    standing_p_per_day REAL,
    UNIQUE(fuel, valid_from)
);

-- Record of every data fetch attempt
CREATE TABLE IF NOT EXISTS sync_log (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at            TEXT NOT NULL,
    source            TEXT NOT NULL,  -- 'sunsynk'|'octopus_elec'|'octopus_export'|'octopus_gas'
    date_fetched      TEXT,
    intervals_written INTEGER,
    status            TEXT NOT NULL,  -- 'ok' | 'partial' | 'error'
    message           TEXT
);
CREATE INDEX IF NOT EXISTS idx_sync_log_run ON sync_log(run_at DESC);
CREATE INDEX IF NOT EXISTS idx_sync_log_source ON sync_log(source, date_fetched);
-- (source) alone carries rowid order, so "latest row for a source" (the
-- /metrics freshness gauges) is an index seek instead of a temp-B-tree sort.
CREATE INDEX IF NOT EXISTS idx_sync_log_src ON sync_log(source);
-- Latest rate per tariff window (_latest_window_rates) was a full scan + sort.
CREATE INDEX IF NOT EXISTS idx_elec_window
    ON electricity_intervals(tariff_window, interval_start);

-- Users / auth (same shape as SOC dashboard)
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'user',
    created_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_login_at TEXT,
    disabled      INTEGER NOT NULL DEFAULT 0,
    totp_secret   TEXT,                          -- base32 TOTP secret (2FA), or NULL
    session_epoch INTEGER NOT NULL DEFAULT 0      -- bump to revoke all sessions
);

-- Encrypted key/value settings (credentials, webhook URL, flask secret, etc.)
CREATE TABLE IF NOT EXISTS settings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    key             TEXT NOT NULL UNIQUE,
    value_encrypted TEXT,
    created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER,
    username    TEXT,             -- denormalised so it survives user deletion
    action      TEXT NOT NULL,
    target_type TEXT,
    target_id   TEXT,
    details     TEXT,             -- json blob with extra context
    ip_address  TEXT,
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_target ON audit_log(target_type, target_id);
-- New-IP login check + failed-login pruning filter on action/user/ip.
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action, user_id, ip_address);

-- One BMS reading a day for the battery state-of-health trend (insights.py).
CREATE TABLE IF NOT EXISTS battery_snapshots (
    date TEXT PRIMARY KEY,
    data TEXT NOT NULL                -- json: capacity_ah, corrected_capacity_ah, …
);

-- Daily shortwave radiation (MJ/m²) from Open-Meteo, kept beyond its 92-day
-- window, to calibrate the solar forecast on this system (solar_forecast.py).
CREATE TABLE IF NOT EXISTS weather_daily (
    date         TEXT PRIMARY KEY,
    radiation_mj REAL
);

-- Grid power quality (power_quality.py): one sample a minute (30 days kept)
-- and the outage / voltage / frequency events derived from them.
CREATE TABLE IF NOT EXISTS grid_samples (
    ts    TEXT PRIMARY KEY,           -- UTC minute
    volt  REAL,
    hz    REAL,
    ok    INTEGER
);
CREATE TABLE IF NOT EXISTS grid_events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    kind    TEXT NOT NULL,            -- outage | overvoltage | undervoltage | frequency
    start   TEXT NOT NULL,
    end     TEXT,
    min_v   REAL, max_v REAL, min_hz REAL, max_hz REAL
);
CREATE INDEX IF NOT EXISTS idx_grid_events_open ON grid_events(kind, end);

-- Per-consumer tokens for the read-only home API (home_tokens.py). The token
-- itself is never stored: only its SHA-256 (tokens are 256-bit random).
CREATE TABLE IF NOT EXISTS home_consumers (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    token_hash   TEXT NOT NULL UNIQUE,
    created_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TEXT,
    last_ip      TEXT,
    requests     INTEGER NOT NULL DEFAULT 0
);
"""


_dir_ready = False


def _ensure_data_dir() -> None:
    """Create the DB's parent directory once per process (cheap guard so conn()
    doesn't issue a mkdir syscall on every open)."""
    global _dir_ready
    if not _dir_ready:
        Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
        _dir_ready = True


@contextmanager
def conn() -> Iterator[sqlite3.Connection]:
    """Yield an autocommit connection; always closed in finally.

    journal_mode=WAL is persistent on the file (set once in init_db), so it is
    NOT re-issued per connection. foreign_keys + synchronous are per-connection
    pragmas; synchronous=NORMAL is the recommended, safe-with-WAL setting that
    speeds writes (a single request opens several connections)."""
    _ensure_data_dir()
    # `timeout` IS SQLite's busy_timeout (sqlite3_busy_timeout): how long a
    # writer waits for a competing write lock before "database is locked".
    # 30 s (configurable) rides out slow-disk backfill/intraday overlap.
    c = sqlite3.connect(DB_PATH, timeout=DB_TIMEOUT_S, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    c.execute("PRAGMA synchronous = NORMAL")
    try:
        yield c
    finally:
        c.close()


# Column additions for upgraders. Applied only when missing. Keep append-only.
_MIGRATIONS: list[tuple[str, str, str]] = [
    ("daily_summaries", "export_estimated", "INTEGER"),
    ("daily_summaries", "import_estimated", "INTEGER"),
    ("users", "totp_secret", "TEXT"),
    ("users", "session_epoch", "INTEGER NOT NULL DEFAULT 0"),
    ("users", "totp_last_counter", "INTEGER"),   # RFC 6238 replay guard
    ("users", "recovery_codes", "TEXT"),          # JSON list of sha256 hashes
]


def _apply_migrations(c: sqlite3.Connection) -> None:
    for table, column, ddl in _MIGRATIONS:
        cols = [r["name"] for r in c.execute(f"PRAGMA table_info({table})")]
        if column not in cols:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def init_db() -> None:
    _ensure_data_dir()
    with conn() as c:
        # WAL is a persistent file-level mode — set it once here rather than on
        # every connection in conn().
        c.execute("PRAGMA journal_mode = WAL")
        c.executescript(SCHEMA)
        _apply_migrations(c)


# ---------- generic interval upserts --------------------------------------

# The four interval tables share an idempotent "insert or update by
# interval_start" pattern. Columns are validated against an allowlist per table
# so callers can pass dicts straight through from the API clients.
_INTERVAL_COLUMNS: dict[str, tuple[str, ...]] = {
    "electricity_intervals": ("interval_start", "interval_end", "consumption_kwh",
                              "tariff_window", "rate_p_per_kwh", "cost_pence"),
    "export_intervals": ("interval_start", "interval_end", "export_kwh",
                         "rate_p_per_kwh", "earned_pence"),
    "gas_intervals": ("interval_start", "interval_end", "consumption_kwh",
                      "rate_p_per_kwh", "cost_pence"),
    "solar_intervals": ("interval_start", "pv_kwh", "battery_charge_kwh",
                        "battery_discharge_kwh", "grid_import_kwh",
                        "grid_export_kwh", "house_load_kwh", "soc_pct"),
}


def upsert_intervals(table: str, rows: Iterable[Mapping[str, Any]]) -> int:
    """Insert/replace interval rows keyed on interval_start. Returns count.

    Unknown keys in each row are ignored; missing optional columns become NULL.
    """
    if table not in _INTERVAL_COLUMNS:
        raise ValueError(f"unknown interval table: {table}")
    cols = _INTERVAL_COLUMNS[table]
    placeholders = ",".join("?" for _ in cols)
    updates = ",".join(f"{c}=excluded.{c}" for c in cols if c != "interval_start")
    # table + cols come from the _INTERVAL_COLUMNS allowlist; values are
    # parameterised. No user input reaches the SQL text.
    sql = (f"INSERT INTO {table}({','.join(cols)}) VALUES({placeholders}) "  # noqa: S608  # nosec B608
           f"ON CONFLICT(interval_start) DO UPDATE SET {updates}")
    # Batch all rows into ONE transaction (the connection is autocommit, so a
    # per-row loop was one implicit commit per row — ~140k commits for a deep
    # backfill). executemany inside an explicit BEGIN/COMMIT is far faster.
    data = [tuple(row.get(col) for col in cols) for row in rows]
    if not data:
        return 0
    with conn() as c:
        c.execute("BEGIN")
        try:
            c.executemany(sql, data)
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise
    return len(data)


def intervals_for_date(table: str, date: str) -> list[sqlite3.Row]:
    """All rows for a YYYY-MM-DD local date, ordered by interval_start.

    Uses a half-open range on interval_start (>= date AND < next day) so the
    UNIQUE(interval_start) index is used — a prefix LIKE forces a full table
    scan in SQLite. ISO timestamps sort lexicographically, so this is correct
    regardless of the stored offset suffix. `table` is from an internal
    allowlist (not user input)."""
    if table not in _INTERVAL_COLUMNS:
        raise ValueError(f"unknown interval table: {table}")
    nxt = (_date.fromisoformat(date) + timedelta(days=1)).isoformat()
    with conn() as c:
        return list(c.execute(
            f"SELECT * FROM {table} WHERE interval_start >= ? AND interval_start < ? "  # noqa: S608  # nosec B608
            f"ORDER BY interval_start", (date, nxt)).fetchall())


def intervals_between(table: str, start: str, end: str) -> list[sqlite3.Row]:
    """Rows with start <= interval_start < end (YYYY-MM-DD bounds work: ISO
    strings sort lexicographically). `table` is from the internal allowlist."""
    if table not in _INTERVAL_COLUMNS:
        raise ValueError(f"unknown interval table: {table}")
    with conn() as c:
        return list(c.execute(
            f"SELECT * FROM {table} WHERE interval_start >= ? AND interval_start < ? "  # noqa: S608  # nosec B608
            f"ORDER BY interval_start", (start, end)).fetchall())


def update_interval_prices(table: str, rows: Iterable[tuple]) -> int:
    """Re-price stored intervals in one transaction. Rows are
    (rate, money, tariff_window_or_None, interval_start); the money column is
    cost_pence (import/gas) or earned_pence (export)."""
    money = {"electricity_intervals": "cost_pence", "gas_intervals": "cost_pence",
             "export_intervals": "earned_pence"}[table]
    window = ", tariff_window=COALESCE(?, tariff_window)" \
        if table == "electricity_intervals" else ""
    data = [r if window else (r[0], r[1], r[3]) for r in rows]
    if not data:
        return 0
    sql = (f"UPDATE {table} SET rate_p_per_kwh=?, {money}=?{window} "  # noqa: S608  # nosec B608
           f"WHERE interval_start=?")
    with conn() as c:
        c.execute("BEGIN")
        try:
            c.executemany(sql, data)
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise
    return len(data)


def has_any_intervals(date: str) -> bool:
    """Whether any source stored anything for a local day."""
    return any(intervals_for_date(t, date) for t in _INTERVAL_COLUMNS)


# ---------- daily summaries -----------------------------------------------

_SUMMARY_COLUMNS = (
    "date", "pv_kwh", "battery_charge_kwh", "battery_discharge_kwh",
    "peak_soc_pct", "end_soc_pct", "grid_import_kwh", "grid_export_kwh",
    "house_load_kwh", "cheap_import_kwh", "day_import_kwh", "peak_import_kwh",
    "import_cost_pence", "export_earned_pence", "standing_elec_pence",
    "gas_kwh", "gas_cost_pence", "standing_gas_pence", "self_sufficiency_pct",
    "net_cost_pence", "sunsynk_ok", "octopus_ok", "data_partial",
    "export_estimated", "import_estimated",
)


def upsert_daily_summary(summary: Mapping[str, Any]) -> None:
    """Insert/update a daily_summaries row keyed on date."""
    if not summary.get("date"):
        raise ValueError("daily summary requires a 'date'")
    cols = [c for c in _SUMMARY_COLUMNS if c in summary]
    placeholders = ",".join("?" for _ in cols)
    updates = ",".join(f"{c}=excluded.{c}" for c in cols if c != "date")
    # cols come from the _SUMMARY_COLUMNS allowlist; values parameterised.
    sql = (f"INSERT INTO daily_summaries({','.join(cols)}) VALUES({placeholders}) "  # noqa: S608  # nosec B608
           f"ON CONFLICT(date) DO UPDATE SET {updates}")
    with conn() as c:
        c.execute(sql, tuple(summary[c] for c in cols))


def get_daily_summary(date: str) -> sqlite3.Row | None:
    with conn() as c:
        return c.execute("SELECT * FROM daily_summaries WHERE date=?",
                         (date,)).fetchone()


def daily_summaries_range(start: str, end: str) -> list[sqlite3.Row]:
    """Summaries with date in [start, end] inclusive, ordered ascending."""
    with conn() as c:
        return list(c.execute(
            "SELECT * FROM daily_summaries WHERE date>=? AND date<=? "
            "ORDER BY date", (start, end)).fetchall())


def summary_date_bounds() -> tuple[str | None, str | None]:
    """(earliest, latest) date present in daily_summaries, or (None, None)."""
    with conn() as c:
        row = c.execute(
            "SELECT MIN(date) AS lo, MAX(date) AS hi FROM daily_summaries"
        ).fetchone()
    return (row["lo"], row["hi"]) if row else (None, None)


def prune_phantom_summaries() -> int:
    """Delete daily summaries for days with no stored interval in ANY table.

    Older versions wrote one for every backfilled day, including the months
    before an account's history began: rows carrying only a standing charge,
    that History and the averages then counted as real (560 of 885 live rows
    in 2026-10 had no import or solar data). Returns rows removed."""
    nxt = "date(daily_summaries.date, '+1 day')"
    clauses = " AND ".join(
        f"NOT EXISTS (SELECT 1 FROM {t} WHERE interval_start >= daily_summaries.date "  # noqa: S608  # nosec B608
        f"AND interval_start < {nxt})" for t in _INTERVAL_COLUMNS)
    with conn() as c:
        cur = c.execute(f"DELETE FROM daily_summaries WHERE {clauses}")  # noqa: S608  # nosec B608
        return cur.rowcount or 0


# ---------- tariff rates --------------------------------------------------

def upsert_tariff_rate(fuel: str, valid_from: str, rate_p_per_kwh: float,
                       valid_to: str | None = None,
                       standing_p_per_day: float | None = None) -> None:
    with conn() as c:
        c.execute(
            """INSERT INTO tariff_rates(fuel, valid_from, valid_to,
                  rate_p_per_kwh, standing_p_per_day)
               VALUES(?,?,?,?,?)
               ON CONFLICT(fuel, valid_from) DO UPDATE SET
                  valid_to=excluded.valid_to,
                  rate_p_per_kwh=excluded.rate_p_per_kwh,
                  standing_p_per_day=excluded.standing_p_per_day""",
            (fuel, valid_from, valid_to, rate_p_per_kwh, standing_p_per_day))


def latest_tariff_rate(fuel: str) -> float | None:
    """Most recent rate for a fuel (e.g. the Flexible counterfactual)."""
    with conn() as c:
        r = c.execute("SELECT rate_p_per_kwh FROM tariff_rates WHERE fuel=? "
                      "ORDER BY valid_from DESC LIMIT 1", (fuel,)).fetchone()
    return r["rate_p_per_kwh"] if r else None


def _utc_iso(ts_iso: str) -> str:
    """Normalise an offset-aware ISO timestamp to 'YYYY-MM-DDTHH:MM:SSZ' (UTC).

    tariff_rates.valid_from is stored verbatim from Octopus (Z-suffixed UTC,
    plus bare YYYY-MM-DD rows for the Flexible counterfactual). Comparing a
    '+01:00' London timestamp against those lexicographically is NOT
    chronological near rate boundaries, so queries normalise to the stored
    format first. Naive/bare inputs pass through unchanged."""
    try:
        dt = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return ts_iso
    if dt.tzinfo is None:
        return ts_iso
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def rate_at(fuel: str, ts_iso: str) -> sqlite3.Row | None:
    """The tariff rate covering a timestamp: latest valid_from <= ts whose
    valid_to is null or > ts. The timestamp may carry any UTC offset."""
    ts = _utc_iso(ts_iso)
    with conn() as c:
        return c.execute(
            """SELECT * FROM tariff_rates
               WHERE fuel=? AND valid_from<=?
                 AND (valid_to IS NULL OR valid_to>?)
               ORDER BY valid_from DESC LIMIT 1""",
            (fuel, ts, ts)).fetchone()


def rates_for_local_day(fuel: str, day: _date) -> list[float]:
    """Unit rates of every stored period overlapping a Europe/London day —
    the day's price spread used to band non-Cosy tariffs for the live view."""
    from zoneinfo import ZoneInfo
    lon = ZoneInfo("Europe/London")
    start = datetime.combine(day, datetime.min.time(), lon)
    end = datetime.combine(day + timedelta(days=1), datetime.min.time(), lon)
    lo, hi = _utc_iso(start.isoformat()), _utc_iso(end.isoformat())
    with conn() as c:
        rows = c.execute(
            """SELECT rate_p_per_kwh FROM tariff_rates
               WHERE fuel=? AND valid_from<? AND (valid_to IS NULL OR valid_to>?)""",
            (fuel, hi, lo)).fetchall()
    return [r["rate_p_per_kwh"] for r in rows]


def tariff_periods_for_local_day(fuel: str, day: _date) -> list[dict]:
    """Stored rate periods overlapping a Europe/London day, shaped like the
    Octopus API ({valid_from, valid_to, value_inc_vat}) for octopus.rate_for."""
    from zoneinfo import ZoneInfo
    lon = ZoneInfo("Europe/London")
    start = datetime.combine(day, datetime.min.time(), lon)
    end = datetime.combine(day + timedelta(days=1), datetime.min.time(), lon)
    lo, hi = _utc_iso(start.isoformat()), _utc_iso(end.isoformat())
    with conn() as c:
        rows = c.execute(
            """SELECT valid_from, valid_to, rate_p_per_kwh FROM tariff_rates
               WHERE fuel=? AND valid_from<? AND (valid_to IS NULL OR valid_to>?)""",
            (fuel, hi, lo)).fetchall()
    return [{"valid_from": r["valid_from"], "valid_to": r["valid_to"],
             "value_inc_vat": r["rate_p_per_kwh"]} for r in rows]


# ---------- sync log ------------------------------------------------------

def sync_log_add(run_at: str, source: str, status: str,
                 date_fetched: str | None = None,
                 intervals_written: int | None = None,
                 message: str | None = None) -> None:
    with conn() as c:
        c.execute(
            """INSERT INTO sync_log(run_at, source, date_fetched,
                  intervals_written, status, message)
               VALUES(?,?,?,?,?,?)""",
            (run_at, source, date_fetched, intervals_written, status, message))


def sync_log_recent(limit: int = 50) -> list[sqlite3.Row]:
    with conn() as c:
        return list(c.execute(
            "SELECT * FROM sync_log ORDER BY id DESC LIMIT ?",
            (limit,)).fetchall())


def _london_today() -> _date:
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("Europe/London")).date()


def prune_sync_log(keep_days: int = 90) -> int:
    """Delete sync_log rows older than keep_days. sync_log is pure fetch
    telemetry written several times an hour, so it grows unbounded otherwise.
    Returns the number of rows removed."""
    cutoff = (_london_today() - timedelta(days=keep_days)).isoformat()
    with conn() as c:
        cur = c.execute("DELETE FROM sync_log WHERE run_at < ?", (cutoff,))
        return cur.rowcount or 0


def prune_audit_failures(keep_days: int = 90) -> int:
    """Delete `user.login_failed` audit rows older than keep_days.

    The rest of audit_log is a durable security record and is kept; failed
    logins are the one attacker-driven row type (up to ~2.9k/day per IP under
    the throttle), so they're bounded to a quarter's worth of evidence."""
    cutoff = (_london_today() - timedelta(days=keep_days)).isoformat()
    with conn() as c:
        cur = c.execute("DELETE FROM audit_log WHERE action='user.login_failed' "
                        "AND created_at < ?", (cutoff,))
        return cur.rowcount or 0


# ---------- users (same API as SOC dashboard) -----------------------------

def list_users() -> list[sqlite3.Row]:
    with conn() as c:
        return list(c.execute("SELECT * FROM users ORDER BY id").fetchall())


def count_users() -> int:
    with conn() as c:
        return int(c.execute("SELECT COUNT(*) FROM users").fetchone()[0])


def get_user_by_username(username: str) -> sqlite3.Row | None:
    with conn() as c:
        return c.execute("SELECT * FROM users WHERE username=?",
                         (username,)).fetchone()


def get_user(user_id: int) -> sqlite3.Row | None:
    with conn() as c:
        return c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()


def insert_user(username: str, password_hash: str, role: str = "user") -> int:
    with conn() as c:
        cur = c.execute(
            "INSERT INTO users(username, password_hash, role) VALUES(?,?,?)",
            (username, password_hash, role))
        return int(cur.lastrowid or 0)


def insert_first_admin(username: str, password_hash: str) -> int | None:
    """Atomically create the first admin — INSERT only while the users table is
    empty, so two concurrent /setup submissions can't BOTH create an admin
    (the check-then-insert race). Returns the new id, or None if a user
    already exists."""
    with conn() as c:
        cur = c.execute(
            """INSERT INTO users(username, password_hash, role)
               SELECT ?, ?, 'admin'
               WHERE NOT EXISTS (SELECT 1 FROM users)""",
            (username, password_hash))
        return int(cur.lastrowid) if cur.rowcount else None


def update_user_password(user_id: int, password_hash: str) -> None:
    with conn() as c:
        c.execute("UPDATE users SET password_hash=? WHERE id=?",
                  (password_hash, user_id))


def set_user_totp(user_id: int, secret: str | None) -> None:
    with conn() as c:
        c.execute("UPDATE users SET totp_secret=?, totp_last_counter=NULL "
                  "WHERE id=?", (secret, user_id))


def set_user_totp_raw(user_id: int, stored: str) -> None:
    """Rewrite the stored TOTP value only (encryption migration) — unlike
    set_user_totp it keeps the replay counter."""
    with conn() as c:
        c.execute("UPDATE users SET totp_secret=? WHERE id=?", (stored, user_id))


def set_totp_last_counter(user_id: int, counter: int) -> None:
    """Record the highest accepted TOTP counter (RFC 6238 §5.2 replay guard)."""
    with conn() as c:
        c.execute("UPDATE users SET totp_last_counter=? WHERE id=?",
                  (counter, user_id))


def claim_totp_counter(user_id: int, counter: int) -> bool:
    """Atomically record a TOTP counter as used — True only for the request
    that advanced it. Read-then-write let two concurrent logins both accept
    the same code (RFC 6238 §5.2)."""
    with conn() as c:
        cur = c.execute(
            "UPDATE users SET totp_last_counter=? WHERE id=? AND "
            "(totp_last_counter IS NULL OR totp_last_counter < ?)",
            (counter, user_id, counter))
        return cur.rowcount == 1


def swap_recovery_codes(user_id: int, expected_json: str, new_json: str) -> bool:
    """Compare-and-swap the stored recovery codes: True only if they were
    still `expected_json` (a concurrent use of the same code loses)."""
    with conn() as c:
        cur = c.execute("UPDATE users SET recovery_codes=? WHERE id=? AND "
                        "recovery_codes=?", (new_json, user_id, expected_json))
        return cur.rowcount == 1


def set_recovery_codes(user_id: int, hashes_json: str | None) -> None:
    with conn() as c:
        c.execute("UPDATE users SET recovery_codes=? WHERE id=?",
                  (hashes_json, user_id))


def bump_session_epoch(user_id: int) -> int:
    """Increment a user's session epoch — invalidates their other signed-cookie
    sessions. Returns the new epoch."""
    with conn() as c:
        c.execute("UPDATE users SET session_epoch=session_epoch+1 WHERE id=?",
                  (user_id,))
        row = c.execute("SELECT session_epoch FROM users WHERE id=?",
                        (user_id,)).fetchone()
    return int(row["session_epoch"]) if row else 0


def update_user_login(user_id: int) -> None:
    with conn() as c:
        c.execute("UPDATE users SET last_login_at=CURRENT_TIMESTAMP WHERE id=?",
                  (user_id,))


def disable_user(user_id: int, disabled: bool = True) -> None:
    with conn() as c:
        c.execute("UPDATE users SET disabled=? WHERE id=?",
                  (1 if disabled else 0, user_id))


def delete_user(user_id: int) -> None:
    with conn() as c:
        c.execute("DELETE FROM users WHERE id=?", (user_id,))


# ---------- settings ------------------------------------------------------

def setting_get(key: str) -> str | None:
    with conn() as c:
        row = c.execute("SELECT value_encrypted FROM settings WHERE key=?",
                        (key,)).fetchone()
    return row["value_encrypted"] if row else None


def setting_set(key: str, value_encrypted: str | None) -> None:
    with conn() as c:
        c.execute(
            """INSERT INTO settings(key, value_encrypted) VALUES(?,?)
               ON CONFLICT(key) DO UPDATE SET
                  value_encrypted=excluded.value_encrypted,
                  updated_at=CURRENT_TIMESTAMP""",
            (key, value_encrypted))


def setting_insert_if_absent(key: str, value: str) -> None:
    with conn() as c:
        c.execute("INSERT OR IGNORE INTO settings(key, value_encrypted) "
                  "VALUES(?,?)", (key, value))


def setting_delete(key: str) -> None:
    with conn() as c:
        c.execute("DELETE FROM settings WHERE key=?", (key,))


def setting_keys_with_prefix(prefix: str) -> list[str]:
    """All settings keys starting with `prefix` (settings is tiny; filtered in
    Python to sidestep LIKE-wildcard escaping)."""
    with conn() as c:
        rows = c.execute("SELECT key FROM settings").fetchall()
    return [r["key"] for r in rows if r["key"].startswith(prefix)]


# ---------- audit log -----------------------------------------------------

def audit_add(user_id: int | None, username: str | None, action: str,
              target_type: str | None = None, target_id: str | None = None,
              details: str | None = None, ip_address: str | None = None,
              created_at: str | None = None) -> None:
    """Append an audit row. `created_at` ('YYYY-MM-DD HH:MM:SS' UTC) defaults
    to SQLite's CURRENT_TIMESTAMP; the login throttle passes its own so the
    rows it counts share its clock."""
    with conn() as c:
        c.execute(
            """INSERT INTO audit_log(user_id, username, action, target_type,
                  target_id, details, ip_address, created_at)
               VALUES(?,?,?,?,?,?,?,COALESCE(?, CURRENT_TIMESTAMP))""",
            (user_id, username, action, target_type, target_id, details,
             ip_address, created_at))


def _backup_date(p: Path) -> _date | None:
    try:
        return _date.fromisoformat(p.name[len("powersoc-"):len("powersoc-") + 10])
    except ValueError:
        return None


def _prune_backups(out_dir: Path, keep: int, today: _date | None = None) -> None:
    """Retention tiers: the newest `keep` dailies, plus Sundays for 8 weeks
    and 1st-of-the-month copies for a year. 14 dailies alone were shorter
    than the three weeks the 2026-10 outage went unnoticed."""
    if keep <= 0:
        return
    today = today or _london_today()
    files = sorted((d, p) for p in out_dir.glob("powersoc-*.db")
                   if (d := _backup_date(p)) is not None)
    keepers = {d for d, _ in files[-keep:]}
    for d, _ in files:
        age = (today - d).days
        if (d.weekday() == 6 and age <= 56) or (d.day == 1 and age < 365):
            keepers.add(d)
    for d, p in files:
        if d not in keepers:
            try:
                p.unlink()
            except OSError:
                pass


def backup_database(keep: int | None = None, dest_dir: str | None = None) -> str:
    """Online SQLite backup (safe while the app runs) into
    BACKUP_DIR/powersoc-YYYY-MM-DD.db, pruned by _prune_backups.

    BACKUP_DIR must live OUTSIDE the data volume (config.BACKUP_DIR /
    POWERSOC_BACKUP_DIR) — ideally on another disk: a backup next to its
    source dies with it, which is how the 2026-09 volume loss took every
    backup along. The copy is written to a temp name, verified with PRAGMA
    quick_check, made 0640 (no world access — it holds encrypted credentials
    + password hashes; group read lets the host operator copy it off-box via
    a setgid backup dir) and only then renamed into place, so a crash
    mid-backup never replaces a good file with a torn one. Also copied to
    POWERSOC_BACKUP_MIRROR_DIR when set. Records `last_backup_at` for
    /api/health and /metrics. Returns the backup path."""
    import os
    import shutil

    import config as _cfg
    out_dir = Path(dest_dir) if dest_dir else Path(_cfg.BACKUP_DIR)
    keep = _cfg.BACKUP_KEEP if keep is None else keep
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"powersoc-{_london_today().isoformat()}.db"
    tmp = dest.with_name(dest.name + ".tmp")
    tmp.unlink(missing_ok=True)
    with conn() as c:
        dst = sqlite3.connect(str(tmp))
        try:
            c.backup(dst)
            # The page copy carries the source's WAL flag; a WAL-mode backup
            # can't even be READ from a read-only location without creating a
            # -shm file. Backups are standalone files: rollback-journal mode.
            dst.execute("PRAGMA journal_mode=DELETE")
            ok = dst.execute("PRAGMA quick_check").fetchone()[0]
        finally:
            dst.close()
    if ok != "ok":
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"backup failed verification: {ok}")
    os.chmod(tmp, 0o640)
    os.replace(tmp, dest)
    _prune_backups(out_dir, keep)
    mirror = getattr(_cfg, "BACKUP_MIRROR_DIR", None)
    if mirror and not dest_dir:
        mirror = Path(mirror)
        mirror.mkdir(parents=True, exist_ok=True)
        mtmp = mirror / (dest.name + ".tmp")
        shutil.copyfile(dest, mtmp)
        os.chmod(mtmp, 0o640)
        os.replace(mtmp, mirror / dest.name)
        _prune_backups(mirror, keep)
    setting_set("last_backup_at", datetime.now(timezone.utc).isoformat())
    return str(dest)


def latest_backup() -> Path | None:
    import config as _cfg
    files = sorted(p for p in Path(_cfg.BACKUP_DIR).glob("powersoc-*.db")
                   if _backup_date(p) is not None)
    return files[-1] if files else None


def _copy_into(src: sqlite3.Connection, dest: Path) -> None:
    d = sqlite3.connect(str(dest))
    try:
        src.backup(d)
    finally:
        d.close()


def restore_database(src: str, accept_new_key: bool = False) -> str:
    """Replace the live DB with a backup file (run with the app STOPPED).

    Order matters — nothing touches the live DB until the replacement is
    fully built and verified:
    1. the source (and any -wal next to it) is COPIED into a work dir inside
       DATA_DIR and opened normally, so writes still in a hot WAL are kept
       (immutable=1 silently dropped them) and a read-only source dir works;
    2. quick_check, and the backup's key check must match the current
       encryption key (else every credential in it is unreadable) unless
       `accept_new_key`;
    3. it is copied to <db>.restore-tmp and verified;
    4. only then is the live DB moved aside to `<db>.pre-restore-<UTC stamp>`
       (never deleted) and the new one renamed into place; a failure there
       moves the old one back.
    Then migrations run, so an older backup is upgraded in place. Returns the
    path the old DB was moved to, or "" if there was none."""
    import os
    import shutil

    import config as _cfg
    src_path = Path(src)
    if not src_path.is_file():
        raise FileNotFoundError(src)
    db_path = Path(DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    work = db_path.parent / f".restore-{stamp}"
    tmp = db_path.with_name(db_path.name + ".restore-tmp")
    work.mkdir()
    try:
        copy = work / "source.db"
        shutil.copyfile(src_path, copy)
        wal = Path(str(src_path) + "-wal")
        if wal.exists():
            shutil.copyfile(wal, str(copy) + "-wal")
        s = sqlite3.connect(str(copy))
        try:
            ok = s.execute("PRAGMA quick_check").fetchone()[0]
            if ok != "ok":
                raise RuntimeError(f"refusing to restore a damaged backup: {ok}")
            try:
                row = s.execute("SELECT value_encrypted FROM settings "
                                "WHERE key='key_check'").fetchone()
            except sqlite3.OperationalError:
                row = None          # pre-settings schema: nothing to check
            if row and row[0] and not _cfg.key_check_matches(row[0]) \
                    and not accept_new_key:
                raise RuntimeError(
                    "this backup was made under a different encryption key — its "
                    "stored credentials would be unreadable. Restore the original "
                    "machine_id (POWERSOC_MACHINE_ID_FILE), or pass "
                    "--accept-new-key to restore anyway and re-enter them")
            tmp.unlink(missing_ok=True)
            _copy_into(s, tmp)
        finally:
            s.close()
        t = sqlite3.connect(str(tmp))
        try:
            ok = t.execute("PRAGMA quick_check").fetchone()[0]
        finally:
            t.close()
        if ok != "ok":
            raise RuntimeError(f"restored copy failed verification: {ok}")
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)

    moved = ""
    sides = ("-wal", "-shm")
    if db_path.exists():
        moved = f"{db_path}.pre-restore-{stamp}"
        shutil.move(str(db_path), moved)
        for suffix in sides:
            side = Path(str(db_path) + suffix)
            if side.exists():
                shutil.move(str(side), moved + suffix)
    try:
        os.replace(tmp, db_path)
    except BaseException:
        if moved:
            shutil.move(moved, str(db_path))
            for suffix in sides:
                if Path(moved + suffix).exists():
                    shutil.move(moved + suffix, str(db_path) + suffix)
        tmp.unlink(missing_ok=True)
        raise
    os.chmod(db_path, 0o600)
    init_db()
    if accept_new_key:
        _cfg.reset_key_check()
    return moved


def audit_list(limit: int = 200, action: str | None = None) -> list[sqlite3.Row]:
    q = "SELECT * FROM audit_log WHERE 1=1"
    params: list[Any] = []
    if action:
        q += " AND action=?"
        params.append(action)
    q += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with conn() as c:
        return list(c.execute(q, params).fetchall())
