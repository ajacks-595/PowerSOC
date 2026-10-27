# PowerSOC — Project Guide

> Self-hosted home-energy dashboard + daily Mattermost brief, aggregating a
> Sunsynk hybrid inverter and Octopus Energy (import / export / gas) into local
> SQLite and a Flask UI.
>
> The original build specification is preserved at `docs/BUILD_PROMPT.md`.

---

## Architecture overview

```
app.py          Flask factory: middleware (auth, CSRF guard), security headers,
                error handlers, CLI (--migrate / --restore)
routes/         web layer: common.py (JSON envelope, input validation, TTL
                cache), payloads.py (data builders), pages.py, metrics.py,
                api.py + admin.py (both on api_bp), home.py (/api/home/*)
auth.py         PBKDF2-SHA256 passwords, session middleware, /auth/login + /setup,
                login throttle (audit_log-backed), cross-origin write guard
config.py       env + .env loading, Settings dataclass, per-machine Fernet crypto
database.py     SQLite schema (WAL), conn() context manager, migrations, CRUD
sunsynk.py      Sunsynk Connect client (sync; RSA-login + /day series integration)
octopus.py      Octopus REST client (import/export/gas, tariffs, discovery)
octopus_graphql.py Octopus Kraken GraphQL: account balance + Octoplus saving sessions
sync.py         orchestration: run_daily(), run_backfill(), daily-summary builder
sync_daily.py   cron entry point (yesterday's sync + posts the brief)
refresh_rates.py cron entry point (daily tariff-rate refresh)
brief.py        daily brief markdown builder + notable-event detection
notifications.py Mattermost webhook delivery
weather.py      Open-Meteo current weather + solar forecast (cached, fails soft)
carbon.py       NESO grid carbon intensity + greenest window (cached, fails soft)
alerts.py       intraday Mattermost alerts (battery-low / high-spend / offline /
                solar under-performance / invalid home tokens), thresholds in Settings
home_tokens.py  per-consumer home-API tokens (hashed), usage + rejected-token tracking
insights.py     baseload, battery ETA, meter-vs-CT, battery earnings, measured capacity
timeline.py     next-24h price/band/carbon per half-hour (/api/timeline, /api/home/timeline)
tariff_watch.py new/ending agreements + fixed-tariff price changes → Mattermost + brief
solar_forecast.py radiation history → kWh calibration, forecast + under-performance alert
power_quality.py grid voltage/frequency samples + outage/excursion events
planner.py      charge plan: v2 cost-minimising 30 h simulation, heuristic fallback
tariff_sim.py   actual usage priced on Octopus's current alternatives
widgets.py      /widget/today — no-auth, iframe-embeddable summary
templates/      Jinja2: base, login, setup, dashboard, history, settings, widget
static/         vendored Chart.js 4, main.js, theme.js, themes.css, main.css
scripts/        deploy-docker.sh (one-command Docker redeploy over SSH)
.github/        CI: ruff, bandit, pytest (py3.12 + 3.14), pip-audit, docker build
```

Data flow: `sync_daily.py` → `sync.run_daily()` fetches a day from both
upstreams → upserts half-hourly rows into `*_intervals` → `compute_daily_summary()`
aggregates into `daily_summaries` → `brief.build_daily_brief()` →
`notifications.post_brief()`. The web UI reads `daily_summaries` (history) and
the interval tables (intraday charts) via the `/api/*` JSON endpoints.

---

## Conventions

- **JSON API shape:** every `/api/*` response is `{success, data, error}` via the
  `ok(data)` / `err(msg, code)` helpers in `app.py`. API routes return `401`
  JSON (not a redirect) when unauthenticated.
- **DB access:** always `with database.conn() as c:` — a WAL-mode, autocommit
  (`isolation_level=None`) connection with `row_factory=sqlite3.Row`. Schema is
  idempotent (`CREATE TABLE IF NOT EXISTS`); column additions go in
  `_MIGRATIONS` as `(table, column, ddl)` tuples.
- **Crypto:** `config.encrypt/decrypt` use a Fernet key derived (PBKDF2 200k,
  memoised once per process) from `POWERSOC_MACHINE_ID` →
  `POWERSOC_MACHINE_ID_FILE` (fails closed if missing) → `/etc/machine-id` →
  `DATA_DIR/.instance_id`. In Docker the seed is `/host/machine_id` on a host
  bind mount, NOT in the data volume. Credentials AND TOTP secrets are
  encrypted; a stolen DB file alone can't decrypt them. Credentials resolve
  settings-table-first, then env var (`config.get_credential`).
- **Auth:** PBKDF2-SHA256, 600k iterations (OWASP 2023), stored
  `pbkdf2$iters$salt$hash`; the count is read from each stored hash, so older
  200k hashes still verify and are upgraded to 600k on the next login.
  Session cookie `powersoc_session` (not Flask's shared default name — other
  apps on the host clobbered it), HttpOnly + SameSite=Lax, 30-day lifetime,
  Secure whenever the request arrived over HTTPS (or `POWERSOC_COOKIE_SECURE=1`). Public surfaces: `/auth/login` + `/auth/logout`
  (`/login` / `/logout` are LAN aliases only — see 2026-10-25), `/setup` (requires the
  one-time setup token from the log / `POWERSOC_SETUP_TOKEN` while no users
  exist), `/widget/*`, `/static/*`, `/api/health`. Token-gated read-only
  surfaces: `/api/home/*`, `/metrics`.
- **CSRF:** `auth.cross_origin_write()` (run first in `before_request`) refuses
  POST/PUT/PATCH/DELETE that the browser marks as not same-origin
  (`Sec-Fetch-Site`, else `Origin`, else `Referer`). SameSite=Lax alone is NOT
  enough here: other ports on the same host are same-SITE. Non-browser clients
  (no such headers) pass. Same-origin `fetch()` from main.js needs nothing.
- **Login throttle:** counted from `audit_log` `user.login_failed` rows
  (shared across workers, survives restarts): per (IP, username) since that
  username's last success from the IP, plus a per-IP ceiling across usernames
  that no success resets. Failure rows carry an app-clock `created_at` so
  tests can freeze `auth.time`; throttled attempts write ≤1 row/IP/minute.
- **Client IP behind a proxy:** `POWERSOC_TRUSTED_PROXIES` (IPs/CIDRs of the
  fronting reverse proxy) honours X-Forwarded-For/-Proto only from those
  peers; never `POWERSOC_TRUST_PROXY=1` on a direct-bind deploy.
- **Missing ≠ zero:** a value no source supplied is `None` in summaries and
  "–" on every surface; an unpriced interval makes the cost `None`; a day's
  firmness is `payloads.cost_status` (confirmed/provisional/unpriced/no_data).
  No summary is written for a day with no stored intervals.
- **Instances:** `config.instance_name()/instance_role()` — a `standby` (with
  `POWERSOC_PEER_URL`) only posts the brief/alerts when its primary is
  unreachable or failed to deliver (`sync._brief_turn`).
- **Input:** POST handlers read bodies via `routes.common._json_body()` (dict or 400) and
  `_body_str()`; numbers via `config.parse_finite()` (rejects NaN/inf). Never
  `request.get_json() or {}` + `.get()` directly.
- **Backups:** `database.backup_database()` → `POWERSOC_BACKUP_DIR` (outside
  the data volume; "isolated" means another DEVICE), verified + 0640, tiers
  (14 daily + 8 Sundays + 12 month-starts), optional
  `POWERSOC_BACKUP_MIRROR_DIR`, Settings → Download latest backup. Failures
  are recorded (`last_backup_error`). `python app.py --restore <file>`
  (refuses a backup from another key unless `--accept-new-key`).
  Dead-man ping: `POWERSOC_HEARTBEAT_URL` — `sync.scheduler_ok()` (data can
  flow + brief not overdue + backup not failing).
- **Key check:** `config.verify_key_check()` at startup; a changed Fernet key
  refuses to start (`POWERSOC_ACCEPT_NEW_KEY=1` to re-key deliberately).
- **Error handling — no silent failures:** every upstream call is wrapped so one
  source failing *degrades* the result (and writes a `sync_log` row) rather than
  aborting. The brief is posted every day, degraded if data is missing.
- **No CDNs / no secrets in source.** All JS/CSS vendored under `static/`.

---

## Known API gotchas

1. **Sunsynk `/day` endpoints return time-series, not scalars.** Daily kWh
   totals are integrated from the power series (trapezoidal, `value × Δt`), not
   read from a scalar field. **Verify the `/day` response shape against a live
   inverter** — see the caveat below.
2. **Sunsynk auth changed.** The maintained `sunsynk-api-client` (v1.0.9) uses a
   modern flow: fetch an RSA public key from `/anonymous/publicKey`, RSA-encrypt
   the password, and `POST /oauth/token/new` with a `nonce` + MD5 `sign`. The
   older plaintext `POST /oauth/token` (with `areaCode`) documented in community
   code is NOT what the live library does. `sunsynk.py` tries the **modern flow
   first, then falls back to the legacy plaintext flow** automatically.
3. **Token re-login.** Sunsynk tokens last ~6 days; cached 0600 in
   `data/sunsynk_token.json`. On 401 the client clears the cache and re-logs in
   once.
4. **Sunsynk platform outages.** 502s during region migrations are common —
   degrade gracefully (the brief still posts).
5. **Octopus data lag.** SMETS2 data may not appear until ~9am (or the next
   day). Never fail the brief — mark the day partial (`<40/48` intervals) and
   post anyway.
6. **Gas is m³, not kWh.** Convert with `kwh = m3 * 1.02264 * 39.5 / 3.6`
   (`octopus.m3_to_kwh`). Meters that already report kWh are handled via the
   `gas_units` setting (Settings → System, or `POWERSOC_GAS_UNITS=kwh`), which
   is plumbed through `Settings.gas_units` → `sync_octopus_day` → `fetch_gas`.
7. **Cosy windows are local time; other tariffs are price-banded.** Cosy is
   classified by Europe/London time-of-day (`octopus.cosy_window`) — never by
   ranking rate values, since price-cap changes can yield >3 values/day.
   `octopus.classify_window` keeps Cosy (and unknown tariffs) time-based but
   bands non-Cosy tariffs (Agile/Go/Flux/flat) into cheap/day/peak by the
   interval's price within that day's spread. Cost is always consumption ×
   the interval's own rate, so it is correct on every tariff regardless of
   band. DST transition days produce 47/49 intervals — handled.
8. **Active tariff selection.** Filter agreements to
   `valid_from <= now AND (valid_to IS NULL OR valid_to > now)`
   (`octopus.select_active_agreement`) — a future-dated agreement may be queued,
   so never take the most-recent-by-date.
9. **Export MPAN has its own serial.** Discovered independently from the import
   meter; reusing the import serial against the export MPAN 404s.
10. **Standing charges are p/day.** Added once per daily summary, not per
    interval.
11. **Saving vs Flexible is approximate.** It assumes the same consumption on
    Flexible (no load-shifting); labelled accordingly in the brief/UI.
12. **Sunsynk inverter clock drift.** If the inverter clock isn't on BST/GMT,
    day boundaries can be off. Check the inverter time in Sunsynk Connect.
13. **Battery sign: realtime is +discharge; the `/day` series is decided per
    day.** The `/day` Battery series was assumed +charge — on the live inverter
    it is +discharge (2026-10-27: every stored day had charge/discharge
    swapped). `sunsynk.battery_sign` now infers the orientation from the SoC
    movement each day, and `sync.fix_battery_orientation()` repaired history
    (runs once per version at startup). Previously documented as:
    **Realtime battery sign is INVERTED vs the `/day` series.** The realtime
    battery endpoint reports `power`/`current` as **+discharge / −charge**, the
    opposite of the signed `/day Battery` series (+charge / −discharge).
    `sunsynk.get_realtime`/`get_indepth` **negate** it to the app-wide
    +charge/−discharge convention so `load_w = pv − battery + grid`,
    `battery_dir`, the flow arrow, and In-Depth all agree. Grid realtime `pac`
    is +import/−export (correct as-is). Verified live 2026-05-27.
14. **Carbon API wants a postcode DISTRICT.** NESO's
    `/regional/intensity/{from}/fw24h/postcode/{code}` answers a bare area
    (`DG`) with HTTP 200 and a body of `null`; a district (`DG1`) returns the
    region's 48 slots. `carbon._outcode()` normalises (full postcode → outward
    half, area → its "1" district) and Settings rejects an area. Forecasts of
    0 g/kWh are real (windy South Scotland) — never treat 0 as missing.
    Verified live 2026-10-21.
15. **Consumption `period_to` is inclusive.** A day's fetch also returns the
    next day's 00:00 reading; `OctopusClient._consumption` drops rows outside
    the requested local day (they were stored unpriced and overwrote the real
    row during a newest-first backfill).
16. **Tariffs change over time.** Discovery keeps every agreement
    (`MeterInfo.agreements`); price a day with `meters.tariff_on(kind, date)`
    / a half-hour with `tariff_at`. `sync.reprice_history()` (Settings →
    Re-price history) re-prices stored intervals from their own agreement.
    A real account can move e.g. Cosy → Cosy Fixed 12M → Cosy within a year.
17. **Direct Debit vs non-DD rows** can both be listed for one period:
    `octopus.prefer_direct_debit`.

### ⚠️ Unverified under the mock-only build

This build mocked all HTTP (no live API calls), so two Sunsynk specifics could
not be confirmed against a real inverter and **should be checked on first live
run**:

- The exact `/day` JSON shape. `sunsynk._parse_day` matches series by label
  substring (`_SERIES_MATCHERS`) and assumes power in kW. If solar/battery
  numbers look wrong, adjust `_SERIES_MATCHERS`, `_DAY_ENERGY_PATH`, and the
  unit handling there first.
- Whether your account accepts the modern or legacy auth flow (the client tries
  both, so login should work either way, but confirm the logs say which).

---

## Database schema rationale

- **Interval tables** (`electricity_intervals`, `export_intervals`,
  `gas_intervals`, `solar_intervals`) store raw half-hourly data, keyed
  `UNIQUE(interval_start)` so re-fetches upsert idempotently.
- **`daily_summaries`** is pre-aggregated (written after a day is collected) so
  Week/Month/Year views never rescan millions of interval rows. It also carries
  `sunsynk_ok` / `octopus_ok` / `data_partial` flags for the brief.
- **`tariff_rates`** caches rate + standing-charge history (refreshed weekly).
- **`sync_log`** records every fetch attempt (ok/partial/error) for debugging.
- **`settings`** holds Fernet-encrypted credentials + the Flask secret key;
  **`users`** / **`audit_log`** mirror the SOC dashboard scheme.

---

## How to extend

- **Add a chart (intraday):** add the series to `_build_day_payload` in
  `routes/payloads.py`,
  then a `makeChart(...)` call + canvas in `dashboard.html` / `main.js`.
- **Add a metric to the daily summary:** add the column to `database.SCHEMA`
  (and `_MIGRATIONS` if upgrading), compute it in `sync.compute_daily_summary`,
  and surface it in `brief.py` / the dashboard cards.
- **Add a notification platform:** add a formatter + sender in
  `notifications.py` alongside `post_brief` (the brief text is platform-neutral
  markdown; most webhooks accept a `{text}` payload).
- **Add a history timeframe:** extend `_period_bounds` + `_build_history_payload`
  in `routes/payloads.py` and add a tab in `history.html`.
- **Add a route:** read endpoints in `routes/api.py`, admin/mutating ones in
  `routes/admin.py` (path-based admin gating: `auth._is_admin_only`), token
  consumers in `routes/home.py`. Read JSON bodies with `_json_body()`.

---

## Deployment & recovery

- **Docker deploy:** `DEPLOY_HOST=user@host [SSH_KEY=…] scripts/deploy-docker.sh`
  ships git HEAD, keeps host config in `HOST_DIR` (default `~/powersoc-host/`:
  compose.env, machine_id, backups/ — outside the volume and outside APP_DIR),
  builds BEFORE touching anything, refuses a new key seed when data exists
  (`NEW_KEY=1` to override), validates `RESTORE_FROM` before stopping
  production, and rolls back to the previous image + source unless the new
  version serves `/api/health` with its version and `credentials_ok`. Tested
  with stub ssh/docker in `tests/test_deploy_script.py`. (A manual
  `docker compose up` without the script uses `./host` next to the compose file
  unless `POWERSOC_HOST_DIR` is set — same layout, different default path.)
- **CI:** `.github/workflows/ci.yml` on push/PR and weekly (catches newly
  published CVEs in pinned deps). Keep its `PIP_VERSION` in step with the
  Dockerfile's pip pin.
- **Host deploy:** `sudo ./deploy.sh` on an Ubuntu 22.04/24.04 host (creates the
  `powersoc` user, `/opt/powersoc`, venv, systemd unit, and `/etc/cron.d/powersoc`).
  Remote from another machine: `DEPLOY_HOST=user@host ./deploy.sh`, with optional
  `SSH_KEY=~/.ssh/key` and `SSH_PORT=` (rsyncs the tree to the target and runs
  the installer there under sudo). `INSTALL_DIR` / `SERVICE_USER` override the
  defaults.
- **Service:** `systemctl {status,restart} powersoc`; logs at
  `/var/log/powersoc.log` (or `journalctl -u powersoc`).
- **Daily sync:** `/etc/cron.d/powersoc` at 06:25 Europe/London runs
  `sync_daily.py` (sync + brief + tariff periods + backup + retention +
  heartbeat); `sync_daily.py --tick` hourly (catch-up, brief safety net,
  alerts); daily rate refresh at 00:05. Run a single day manually:
  `venv/bin/python sync_daily.py 2026-05-20`.
- **DB:** SQLite in `data/powersoc.db` (WAL). Re-run a migration/init with
  `python app.py --migrate`; restore with `python app.py --restore <backup>`
  (app stopped). Nightly verified backups go to `POWERSOC_BACKUP_DIR`.
- **Re-backfill:** Settings → Backfill missing data (per source, only gaps /
  unpriced half-hours) or `POST /api/backfill/start`; Re-price history
  (`POST /api/backfill/reprice`) for prices only.

---

## Open items / roadmap

- Verify Sunsynk `/day` shape + auth against a live inverter (see caveat above).
- Optional per-meter gas-unit detection (m³ vs kWh) instead of the m³ default.
- Tariff override UI for when auto-discovery can't find a code.

The first-run experience is a guided multi-step `/setup` wizard (admin →
Sunsynk+Test → Octopus+Discover → notifications+Test → review & backfill). It
posts JSON and advances client-side; a no-JS form post falls back to the
Settings page (`/settings?setup=1`). All credentials are enterable through the
UI and stored Fernet-encrypted — env vars are an optional convenience only.

---

## Project-specific repo review additions

_(Append new test patterns/bug classes here with a date, a rationale, and the
test that enforces it.)_

### 2026-06-04 — Non-hermetic, wall-clock-dependent tests
`tests/test_alerts.py::test_check_alerts_dedups_per_day` hard-coded an absolute
date (`2026-05-26`) and exercised `alerts.check_alerts`, which calls
`_prune_old(keep_days=7)` against the *real* `datetime.now()`. Once that date
aged past 7 days the dedup record was pruned mid-test and the suite went red with
no code change. **Rule:** any test touching `alerts`, `sync` schedulers, or
`*_prune_*`/retention logic must freeze the clock (monkeypatch the module's
`datetime.now`/`LONDON`) or use a date computed relative to `datetime.now()` —
never a literal calendar date. Add a retention test that asserts *today's* key
survives while an older-than-`keep_days` key is the only one pruned.

### 2026-06-04 — Open-redirect `next=` param regression test
`auth.login_submit` sanitises `next` with the naive `startswith("/")` /
`startswith("//")` filter, which misses the browser backslash-normalisation
bypass (`/\evil.com` → `//evil.com`). Add a parametrised test asserting `next` of
`//evil.com`, `/\evil.com`, `/\/evil.com`, `https://evil.com`, `javascript:` all
collapse to `/`.

### 2026-06-04 — Public endpoints must not Set-Cookie
`before_request` setting `session.permanent = True` unconditionally emits a
`Set-Cookie` (+ `Vary: Cookie`) on every response, including `/api/health` and
`/widget/*`. Add a smoke test asserting public GETs carry no `Set-Cookie`, and an
`iter_rules()` "every GET route returns < 500" sweep against `auth_client`.

### 2026-06-10 — Typed error boundaries on the API clients
`requests.ConnectionError`/`Timeout`/JSON-decode errors escaped `sunsynk._get`
and `octopus._get` untyped (tenacity `reraise=True` re-raises the raw error),
so every caller's `except SunsynkError/OctopusError` missed them: `/api/live`
500'd raw (observed live during a DNS blip) and the daily sync could abort.
**Rule:** the clients wrap ALL `requests.RequestException` into
`SunsynkConnectionError`/`OctopusConnectionError` at the boundary; new client
code must preserve this. Regression tests simulate a `ConnectionError` through
`/api/live` and `sync.run_daily` and assert degradation, not 500/abort. The
conftest network kill switch raises `ConnectionError` for any un-mocked HTTP,
so missing boundary handling surfaces as a 500 in the route sweep.

### 2026-06-10 — Login fallback must never downgrade on transient errors
`_login_modern` raised `SunsynkAuthError` for ANY non-OK status, and `login()`
fell back to the legacy PLAINTEXT-password flow on it — so a routine 502/429
re-sent the raw password. **Rule:** auth-flow fallbacks may trigger only on a
definitive rejection (2xx-with-success:false / 4xx auth status), never on
5xx/429/network errors. Test: 502 on publicKey ⇒ raises non-auth SunsynkError
and the legacy endpoint is never called.

### 2026-06-10 — Singleton-effect dedup keys need BOTH writers checking
The daily brief double-posted: `post_brief_if_ready` (safety net) had no
time-of-day gate and `run_daily_and_notify` (master) wrote but never READ the
dedup key. **Rule:** any at-most-once-per-day side effect must (a) have every
writer check the dedup key under a shared lock before acting, and (b) gate
catch-up paths on the time window they're catching up FOR. Tests assert the
midnight tick never posts and the master skips an already-posted date.

### 2026-06-10 — Same-tzinfo datetime subtraction is wall-clock
`expected_intervals` computed `(start + 1day) - start` on two Europe/London
datetimes and always got 24h — Python does NAIVE arithmetic when both operands
share a tzinfo, so DST days never yielded 46/50. **Rule:** duration maths
across DST must go through `.timestamp()` (or convert to UTC first); assert
2026-03-29→46 and 2026-10-25→50 in tests. (Caught by the new regression test,
not by review.)

### 2026-06-10 — Test isolation is enforced, not opt-in
`tests/conftest.py::_isolate` (autouse) now provides: a fresh DB per test
(previously the opt-in `tmp_db` — a notifications test silently used the real
DB and could post to a real webhook via env vars), credential env-var scrub,
module-level cache resets (app/_snap, weather, carbon, graphql, widgets, auth
throttle, backfill state), and a network kill switch raising
`requests.ConnectionError` on any un-mocked HTTP. New module-level caches MUST
be added to the reset list. Seed "today" with `sync.today()` (London), never
`date.today()` (host TZ — red between 23:00–00:00 UTC in BST).

### 2026-09-30 — Backups must leave the failure domain they protect
The production container, image AND `powersoc-data` volume vanished from the
Docker host (cause unknown; the host had rebooted repeatedly). `backup_database()`
wrote every nightly backup to `/data/backups` — inside the lost volume — along
with the `.instance_id` Fernet key seed, so nothing was recoverable except two
stale May host copies. **Rule:** a backup that shares a volume/host with its
source is not a backup. Backups need a destination outside the data volume
(`POWERSOC_BACKUP_DIR` bind-mounted to another disk/host, or pushed off-box),
and the restore path must be tested. Test: backup → restore into a fresh
DATA_DIR with the same `POWERSOC_MACHINE_ID` → `init_db()` → credentials
decrypt and `count_users()` matches. Also: nothing off-box noticed the outage —
add an external liveness check on `/api/health` (or a dead-man ping after the
brief).

### 2026-09-30 — Wall-clock arithmetic regressed in the scheduler
`sync._seconds_until` does `target - now` on two Europe/London datetimes (the
2026-06-10 rule above), so the master run fires at 05:25 AND 06:25 on
2026-10-25 and at 07:25 on 2027-03-28. **Rule:** every sleep/schedule duration
goes through `.timestamp()`. Test: simulate the loop across both DST
transitions and assert exactly one run per local day at 06:25.

### 2026-09-30 — No per-call key derivation on hot paths
`config.fernet()` re-ran PBKDF2(200k) on every encrypt/decrypt: `Settings.load()`
cost 374 ms (14 derivations) and every `/api/home/*` + `/metrics` request paid
26 ms just to decrypt the token. **Rule:** derive process-constant keys once
(memoise on machine-id + salt). Test: `Settings.load()` with all 14 creds set
performs ≤1 PBKDF2 per process (patch `pbkdf2_hmac` and count calls).

### 2026-09-30 — Input-shape fuzzing: non-ASCII, non-finite, wrong JSON type
Three 500s found by probing, none covered by tests: (a) TOTP code in Unicode
digits (`isdigit()` accepts them) → `hmac.compare_digest` TypeError; (b) a
JSON *array* body → `body.items()` AttributeError; (c) `float("nan"/"inf")`
accepted for numeric settings → `/api/roi` emits invalid JSON (`NaN`) or
OverflowError-500s permanently. **Rule:** validate `isascii()` before any
`compare_digest`, require `isinstance(body, dict)`, reject non-finite floats
(`math.isfinite`). Test: sweep every POST route with `[]`, `"x"`, `null`
bodies (expect 4xx); fuzz every compare_digest site with non-ASCII; assert
all JSON responses parse with `allow_nan`-strict decoding.

### 2026-09-30 — Example config must round-trip through our own loader
`.env.example` says "copy to .env", but `load_dotenv_file` does not strip
inline comments, so blank fields become their comment text — e.g.
`POWERSOC_MACHINE_ID='# RECOMMENDED on Docker: …'` derives the Fernet key from
a string published in the repo. Test: `load_dotenv_file(".env.example")` →
no resulting value starts with `#` (fix the loader or the file).

### 2026-09-30 — Upstream call budgets
One `run_daily()` issues 21 Octopus GETs where ~8 carry data (account
discovery re-probes every serial × 3 dates each run; `_METER_CACHE_KEY` is dead
code); one intraday tick issued 63. **Rule:** discovery/counterfactual lookups
are cached (daily), not per-run. Test: mock `OctopusClient._get`, count calls
for `run_daily()` and assert a budget.

### 2026-10-02 — Restores run from places the app cannot write
The first live restore failed: the prod backup was a WAL-mode SQLite file in a
host dir the container may only traverse, and SQLite needs to create a `-shm`
file to read a WAL database. **Rule:** open restore sources with
`?mode=ro&immutable=1`, and write backups with `journal_mode=DELETE` so they
are standalone files. Test: restore a WAL-mode source from a 0555 directory.

### 2026-10-02 — Remote scripts fed on stdin vs. docker
`scripts/deploy-docker.sh` pipes its remote half through `ssh … bash -s`;
`docker compose run` attaches stdin by default and silently swallowed the rest
of the script (the restore ran, the container never started). Also, ssh
flattens arguments, so an empty one vanishes (`set -u` → unbound `$3`).
**Rule:** every docker call in a stdin-fed script gets `-T`/`</dev/null`, and
optional positional args are read as `${N:-}`. Verify a deploy by its
end-of-script health line, not by the absence of errors.

### 2026-10-07 — SameSite is per SITE, and a site ignores the port
Gemini flagged "no CSRF protection"; the real gap was narrower but real: a
form / text-plain POST from ANY other app on this host (other self-hosted apps
on other ports) is same-site, so the Lax cookie rode along and the request
was acted on (start backfill, disable a user, regenerate the home token —
reproduced). **Rule:** state-changing routes are guarded by
`auth.cross_origin_write()`; don't add mutating routes that bypass
`before_request`. Tests: same-site / cross-site / Origin-mismatch / Origin:null
writes → 403; same-origin and header-less clients → allowed.

### 2026-10-07 — Weekly CI audit
Werkzeug 3.1.8's CVE-2026-102598 (Windows-only) was published 2026-09-29 and
only surfaced in a later manual audit. CI now runs pip-audit weekly on the
pinned manifests — treat a red scheduled run as a dependency-bump ticket.

### 2026-10-07 — Missing data must never render as zero
The functional review found "phantom" days: `run_daily` with no source
configured (or both failing) writes an all-zero summary, and the dashboard
then shows "No data" *and* "£0.00 — confirmed". The same missing≠zero bug
appears for unsettled gas (card + brief) and reconciliation months with 0
days. **Rule:** a value the app doesn't have renders as "–"/"pending", never
0; summaries carry provenance flags (`*_ok`, `*_estimated`, `data_partial`)
and every surface must honour them (incl. the "confirmed" label, which must
also consider `import_estimated` and `data_partial`). Test: unconfigured
run_daily writes nothing; a `!sunsynk_ok && !octopus_ok` row renders "–".

### 2026-10-07 — JSON turns Infinity/NaN into null
Settings → System sent `parseFloat("1e999")` = Infinity; `JSON.stringify`
emits `null`, which the server treats as "clear" — the setting was erased
and the UI said "Saved ✓". **Rule:** validate numeric inputs client-side
(`el.validity.badInput`, `Number.isFinite`) and refuse to submit; never let an
edited-but-invalid field serialise as null. Test (browser): 11500 → `1e999` →
error shown, stored value unchanged.

### 2026-10-07 — Spring-forward needs real elapsed time too
The autumn fix kept record order, but on the spring day Sunsynk clock strings
jump 00:55 → 02:00 and the integrator counted 65 min (+3–5% load). **Rule:**
integrate power series on real instants (clock times anchored to
Europe/London, fold-aware), not on wall-clock deltas. Test: 2026-03-29 and
2026-10-25 series integrate to the real-time truth (±1%).

### 2026-10-07 — Functional review harness + browser E2E
Scanners and unit tests were green while five user-visible bugs sat in the
dashboard JS (stale subtitles, a CO₂ panel that never renders on load because
it races `loadDay()` for shared state, mislabels). They were found only by
driving the UI. **How:** a local review harness (not committed — `.repo-review/`
is gitignored): a fake transport behind the real clients (physical day model,
settlement lag, real-account shape), a scenario runner (pipeline +
cross-surface consistency vs ground truth) and Playwright E2E (use the cached
`~/.cache/ms-playwright/chromium_headless_shell-*/…/chrome-headless-shell` via
`executable_path`, `--no-sandbox`, `timezone_id="Europe/London"` — a UTC
browser produces false timezone findings). Server and browser must run in the
SAME sandboxed command (separate network namespaces otherwise).

### 2026-10-07 — Digest pins need a refresh cadence
Pinning `python:3.12-slim` by digest (2026-10-02) also froze Debian security
updates (pcre2 already has a fix). **Rule:** refresh the digest deliberately on
a cadence (monthly or when CI's base-image check reports a newer one), rebuild,
retest, refresh constraints.txt.

### 2026-10-14 — CSRF guard must be tested the way the LAN deploy is reached
v1.4.1 403'd every login on `http://<LAN IP>:8073`. Browsers send
`Sec-Fetch-Site` only to secure origins (HTTPS, localhost, 127/8), so on plain
HTTP the guard falls back to `Origin` — and our `Referrer-Policy: no-referrer`
makes a same-origin `<form>` POST carry `Origin: null` + no Referer (Fetch
spec). Unit tests set `Sec-Fetch-Site` by hand; the E2E ran on localhost; both
masked it. **Rule:** Referrer-Policy stays `same-origin` (never `no-referrer`
while an Origin fallback exists), and browser checks of form posts use a
non-loopback hostname over HTTP — e.g. Chromium with
`--host-resolver-rules=MAP powersoc.test 127.0.0.1` + `--no-proxy-server`
(unset the sandbox `*_PROXY` vars). Test: `test_login_form_over_plain_http_is_not_blocked`.

### 2026-10-21 — "Up" is not "ingesting": health must cover data flow
After the 2026-09-30 restore every stored credential failed to decrypt, so
both sources read as unset and nothing synced for three weeks — while
`/api/health` said ok, the heartbeat pinged ok hourly, Settings showed blank
fields, and `run_daily` wrote an all-zero summary every day (the 10-07 rule
"unconfigured writes nothing" had been recorded but never implemented — a rule
in this file is not a fix; check it has a test). Separately the CO₂ panel had
never rendered: the default carbon outcode was an area (gotcha 14), and the
failure wasn't cached, so it re-requested ~36×/day. **Rule:** liveness signals
(health JSON, heartbeat, the daily brief) must report "can data flow"
(`sync.ingest_ok()`, `config.unreadable_credentials()`), not just "process
up"; failed upstream lookups are back-off cached. Tests:
`tests/test_review_2026_10_21.py` (unreadable creds surfaced in health/
Settings/brief/heartbeat; unconfigured run_daily + backfill write nothing;
carbon district + null body + failure caching). Browser-checked the Settings
warning with seeded undecryptable values (admin user — /settings redirects
non-admins).

### 2026-10-25 — The proxy owns some paths on the app's domain
Behind Cloudron's App Proxy the login page never loaded: sign in to Cloudron,
reload, same Cloudron page, forever. Cloudron's proxy auth answers `/login`
and `/logout` on the app's domain itself, and PowerSOC sent anonymous users to
`/login?next=/`, so the request never reached the app (its access log showed
only `GET /` from the proxy). Separately, the investigation started on the
wrong box: the proxy's upstream was a second, older PowerSOC on another host
(see the deploy notes) — check which instance a proxy actually targets (its
access log, `/api/health` version) before debugging the one you deployed.
**Rule:** the app's own pages live under `/auth/` (`auth.LOGIN_PATH` /
`LOGOUT_PATH`; `/login` and `/logout` remain aliases for LAN bookmarks) and no
redirect may target a path a fronting proxy reserves. Tests:
`tests/test_review_2026_10_25.py` (anonymous redirect sweep never targets
`/login`/`/logout`; login through a proxied Host with HTTPS fetch metadata).
Browser-checked over plain HTTP on a non-loopback name.


### 2026-10-25 — A 200 from a login wall is not a delivery
The Pi instance logged `Brief posted: ok=True (200 <!DOCTYPE html>…` every
day for weeks: Cloudflare Access in front of the Mattermost host answered the
webhook POST with a redirect, requests followed it to the sign-in page, and
any 2xx counted as sent. **Rule:** a webhook delivery is a 2xx whose final
host is the webhook's host and whose body is not an HTML page
(`notifications._not_a_webhook_reply`); report only the redirect's HOST
(the webhook secret is in the URL). Tests: `tests/test_notifications.py`.

### 2026-10-26 — Recorded rules must arrive with their test
Three 2026-10-07 lessons (spring-forward integration, invalid number inputs,
missing≠zero rendering) sat in this file for weeks, unimplemented — the
10-21 lesson had already said "a rule in this file is not a fix". **Rule:**
every lesson added here names the test that enforces it; the review checks
each named test exists. Tests: `tests/test_review_2026_10_26*.py`.

### 2026-10-26 — Price history on the agreement of the day
Discovery kept only today's agreement, so a backfill priced every past day
on the current tariff (the live account changed tariff twice), and a failed
re-discovery synced with NO tariff — NULL prices, the day marked settled,
the stored tariff code cleared. **Rule:** per-day/per-half-hour tariffs come
from `MeterInfo.agreements`; a lookup failure falls back to the stale cache;
a known stored price is never overwritten with an unknown one
(`sync._keep_stored_prices`). Tests: agreement history, stale-cache
fallback, rate-blip, reprice (`test_review_2026_10_26.py`).

### 2026-10-26 — Liveness covers every daily duty
The Pi's brief was refused by a login wall for weeks and only an INFO line
said so; backups could fail silently. **Rule:** each daily duty (sync, brief,
backup) records an outcome that reaches `/api/health`, the heartbeat
(`sync.scheduler_ok`) and the next brief. Tests: `test_review_2026_10_26_brief.py`,
`test_review_2026_10_26_ops.py`.

### 2026-10-26 — Shell that touches production gets a harness
`deploy-docker.sh` could generate a new key seed over existing data, stop
production for a typo'd restore path, and report "Deployed" for a version
whose credentials no longer decrypted — none of it was ever executed in a
test. **Rule:** deploy/restore scripts run under `tests/test_deploy_script.py`
(stub `ssh`/`docker`, scripted outcomes); every refusal and the rollback path
has a case.

