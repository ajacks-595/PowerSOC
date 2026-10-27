# PowerSOC — Claude Code Build Prompt

> **Historical document.** This is the original specification PowerSOC was
> first generated from (May 2026), kept for context. The code has moved on
> since; where this file disagrees with the README or `CLAUDE.md`, they win.
>
> Hand this file to Claude Code in the empty `powersoc/` project directory.
> Run with: `claude --dangerously-skip-permissions`
> Recommended: run inside a devcontainer for blast-radius containment.

---

## Mission

Build **PowerSOC** — a self-hosted home energy monitoring dashboard and daily
briefing tool. It aggregates data from a **Sunsynk hybrid solar inverter**
(via the Sunsynk Connect cloud API) and **Octopus Energy** (electricity import
on Cosy Octopus, electricity export on Outgoing Octopus, and gas), stores
everything locally in SQLite, serves a Flask web dashboard, and posts a daily
morning summary to Mattermost.

This is a **GitHub-ready, generic-first build**. No hardcoded hostnames,
usernames, or account numbers anywhere in source. All site-specific values
come from environment variables or the settings UI. The README must be written
for a stranger forking this for their own home.

---

## Constraints and non-negotiables

- **Python 3.11+**, Flask 3, SQLite in WAL mode — matches the conventions of
  the author's existing SOC dashboard.
- **No CDN dependencies.** Vendor all JS/CSS locally under `static/`. No
  external font loads. The dashboard must work air-gapped.
- **All credentials via env vars or the settings UI.** Never in source or
  committed config files. `.env.example` in the repo root documents every
  variable with a description and example value (no real values).
- **GitHub-ready from day one.** `.gitignore` covers `.env`, `data/`,
  `*.db`, `venv/`, `__pycache__/`. Generic README. No personal domains or
  hostnames in source.
- **UI-first configuration.** The first-run `/setup` wizard collects all
  credentials and discovers meters/tariffs automatically. No manual editing of
  config files required post-install.
- **Embeddable widgets.** Key summary panels must be servable as standalone
  iframe-embeddable pages (e.g. `GET /widget/today`) so they can drop into
  other dashboards (Homarr, Homer, custom pages) without auth.
- **Port 8073.** Document this in README and the systemd unit. Choose 8073
  unless there is a concrete conflict reason to use another port in the 8070
  range, and document your choice.
- **Standalone systemd service** (`powersoc.service`). Include a working
  `deploy.sh` that a user can run on any Ubuntu 22.04+ host.
- **No silent failures.** Every API call that fails must log the error and
  post a degraded brief rather than crashing. The Mattermost message is sent
  regardless of whether one or both upstream APIs are unavailable.

---

## Pre-build steps (do these before writing application code)

1. **Verify Sunsynk API paths** by installing and reading the actual
   `sunsynk-api-client` library source:
   ```
   pip install sunsynk-api-client
   python -c "import sunsynk; import inspect; print(inspect.getfile(sunsynk))"
   ```
   Open the library's `client.py` (or equivalent) and record:
   - The exact login endpoint URL and request body shape
   - The exact daily-totals endpoint paths and parameter names
   (`column` vs `columns`, `lan` vs `language`, etc.)
   - Which response fields carry scalar daily kWh totals (etoday,
     etodayFrom, etodayTo, etodayChg, etodayDischg, dailyUsed) and on
     which endpoints they appear
   - Whether `/api/v1/plant/energy/{plantId}/day` returns all four series
     in one call (preferred over four separate `/day` calls if it does)

   **Critical finding from prior review:** the `/day` chart endpoints return
   time-series records for drawing power curves. Daily kWh scalar totals may
   live on `/realtime` endpoints OR may be embedded in the chart series
   response — this is inconsistent across community implementations. Verify
   against the library source and, if possible, a live API call, before
   writing the client. Do not assume `pv.get("etoday", 0)` works on a
   chart-series response — it will silently return 0.

2. **Install dependencies** before writing application code:
   ```
   pip install flask requests tenacity python-dateutil sunsynk-api-client
   ```

3. **Initialise git** and create `.gitignore` before writing any files.

---

## Architecture

### Directory layout

```
powersoc/
├── app.py                  # Flask app, routes, auth middleware
├── auth.py                 # PBKDF2-SHA256 hashing, session middleware, setup wizard
├── database.py             # SQLite schema, CRUD helpers, idempotent migrations
├── config.py               # Settings dataclass, env var loading, first-run detection
├── sunsynk.py              # Sunsynk Connect API client
├── octopus.py              # Octopus Energy REST API client (elec + gas)
├── sync.py                 # Data fetch/backfill orchestration, scheduler
├── notifications.py        # Mattermost webhook formatter and delivery
├── brief.py                # Daily summary builder (calculations + message assembly)
├── widgets.py              # Embeddable widget routes (no auth required)
├── requirements.txt
├── deploy.sh               # Rsync + venv + systemd install script
├── powersoc.service        # systemd unit
├── .env.example            # All env vars documented, no real values
├── .gitignore
├── README.md
├── CLAUDE.md               # This file (committed to repo)
├── templates/
│   ├── base.html           # Theme toggle, nav, common head
│   ├── setup.html          # First-run wizard
│   ├── login.html
│   ├── dashboard.html      # Main view: today's summary + charts
│   ├── history.html        # Historical view with D/W/M/Y tabs
│   ├── settings.html       # Credentials, tariff config, notification config
│   └── widget_today.html   # Embeddable today summary (no chrome)
├── static/
│   ├── css/
│   │   ├── main.css        # Layout + components
│   │   └── themes.css      # dark + light as CSS custom properties
│   └── js/
│       ├── main.js         # PowerSOC namespace, chart init, tab logic
│       └── chart.min.js    # Vendored Chart.js v4
└── data/                   # gitignored — SQLite DB lives here
```

### Hosts and deployment

PowerSOC is host-agnostic — `deploy.sh` targets whatever host the user
specifies. The service user should be configurable (default: `powersoc`).
Document in README that the app has been tested on Ubuntu 22.04 LTS and
Ubuntu 24.04 LTS.

### Environment detection

`config.py` reads all settings from environment variables (loaded from a
`.env` file sourced by the systemd unit). No hardcoded defaults for
credentials. Sensible defaults only for non-secret settings (port, log level,
data directory path).

---

## Database schema

Use `CREATE TABLE IF NOT EXISTS` throughout. Maintain a `_MIGRATIONS` list
for `ALTER TABLE` column additions (same pattern as SOC dashboard).

### Tables

**`electricity_intervals`** — half-hourly electricity import from Octopus
```sql
CREATE TABLE electricity_intervals (
    id INTEGER PRIMARY KEY,
    interval_start TEXT NOT NULL,   -- ISO 8601, Europe/London
    interval_end   TEXT NOT NULL,
    consumption_kwh REAL NOT NULL,
    tariff_window  TEXT NOT NULL,   -- 'cheap' | 'day' | 'peak'
    rate_p_per_kwh REAL,            -- p/kWh inc VAT at time of interval
    cost_pence     REAL,            -- consumption_kwh * rate_p_per_kwh
    UNIQUE(interval_start)
);
```

**`export_intervals`** — half-hourly export via Outgoing Octopus MPAN
```sql
CREATE TABLE export_intervals (
    id INTEGER PRIMARY KEY,
    interval_start  TEXT NOT NULL,
    interval_end    TEXT NOT NULL,
    export_kwh      REAL NOT NULL,
    rate_p_per_kwh  REAL,           -- SEG rate at time of interval
    earned_pence    REAL,
    UNIQUE(interval_start)
);
```

**`gas_intervals`** — half-hourly gas consumption
```sql
CREATE TABLE gas_intervals (
    id INTEGER PRIMARY KEY,
    interval_start  TEXT NOT NULL,
    interval_end    TEXT NOT NULL,
    consumption_kwh REAL NOT NULL,
    rate_p_per_kwh  REAL,
    cost_pence      REAL,
    UNIQUE(interval_start)
);
```

**`solar_intervals`** — half-hourly Sunsynk data (derived from /day series)
```sql
CREATE TABLE solar_intervals (
    id INTEGER PRIMARY KEY,
    interval_start      TEXT NOT NULL,
    pv_kwh              REAL,       -- solar generation this interval
    battery_charge_kwh  REAL,
    battery_discharge_kwh REAL,
    grid_import_kwh     REAL,
    grid_export_kwh     REAL,
    house_load_kwh      REAL,
    soc_pct             REAL,       -- battery state of charge
    UNIQUE(interval_start)
);
```

**`daily_summaries`** — pre-aggregated daily totals (written after each day's
data is fully collected; used for W/M/Y views without re-scanning intervals)
```sql
CREATE TABLE daily_summaries (
    id INTEGER PRIMARY KEY,
    date TEXT NOT NULL UNIQUE,      -- YYYY-MM-DD
    -- Solar
    pv_kwh              REAL,
    battery_charge_kwh  REAL,
    battery_discharge_kwh REAL,
    peak_soc_pct        REAL,
    end_soc_pct         REAL,
    -- Electricity
    grid_import_kwh     REAL,
    grid_export_kwh     REAL,
    house_load_kwh      REAL,
    cheap_import_kwh    REAL,
    day_import_kwh      REAL,
    peak_import_kwh     REAL,
    import_cost_pence   REAL,
    export_earned_pence REAL,
    standing_elec_pence REAL,
    -- Gas
    gas_kwh             REAL,
    gas_cost_pence      REAL,
    standing_gas_pence  REAL,
    -- Derived
    self_sufficiency_pct REAL,      -- (1 - grid_import / house_load) * 100
    net_cost_pence      REAL,       -- import_cost + standing - export_earned
    sunsynk_ok          INTEGER,    -- 1 if Sunsynk data present
    octopus_ok          INTEGER,    -- 1 if Octopus data present
    data_partial        INTEGER     -- 1 if fewer than 40/48 intervals
);
```

**`tariff_rates`** — cached rate history (refreshed weekly)
```sql
CREATE TABLE tariff_rates (
    id INTEGER PRIMARY KEY,
    fuel            TEXT NOT NULL,  -- 'electricity_import' | 'electricity_export' | 'gas'
    valid_from      TEXT NOT NULL,
    valid_to        TEXT,
    rate_p_per_kwh  REAL NOT NULL,
    standing_p_per_day REAL,
    UNIQUE(fuel, valid_from)
);
```

**`sync_log`** — record of every data fetch attempt
```sql
CREATE TABLE sync_log (
    id INTEGER PRIMARY KEY,
    run_at      TEXT NOT NULL,
    source      TEXT NOT NULL,  -- 'sunsynk' | 'octopus_elec' | 'octopus_export' | 'octopus_gas'
    date_fetched TEXT,
    intervals_written INTEGER,
    status      TEXT NOT NULL,  -- 'ok' | 'partial' | 'error'
    message     TEXT
);
```

**`users`**, **`settings`**, **`audit_log`** — same pattern as SOC dashboard.

---

## Sunsynk client (`sunsynk.py`)

### Authentication

OAuth2 Resource Owner Password Credentials grant against
`https://api.sunsynk.net/oauth/token`:

```json
{
  "areaCode": "sunsynk",
  "client_id": "csp-web",
  "grant_type": "password",
  "source": "sunsynk",
  "username": "<SUNSYNK_USERNAME>",
  "password": "<SUNSYNK_PASSWORD>"
}
```

Response includes `access_token` (Bearer, ~6 day lifetime) and
`refresh_token`. For the daily cron, re-login on every run. Cache the token
to a 0600 file under `data/` for reuse within the same run.

Validate response with `body.get("code") == 0 and body.get("success")` —
check both fields as the community has seen inconsistency in which one is
set on error.

### Retry policy

Wrap `_get()` with `@retry(stop=stop_after_attempt(3),
wait=wait_exponential(min=2, max=30), retry=retry_if_exception(is_retryable))`
where `is_retryable` returns True for connection errors, timeouts, and HTTP
5xx/429, and False for 401/403/404 (which are auth or config problems, not
transient).

Handle 401 by clearing the cached token and re-logging in once before
retrying.

### Daily data extraction — CRITICAL

**Do not rely on scalar fields (etoday, etodayChg, etc.) from the `/day`
chart endpoints without first verifying they exist in the actual API
response.** These endpoints return time-series for charting; scalar totals
may or may not be present depending on the `column` parameter and API version.

**Preferred approach:** Use `GET /api/v1/plant/energy/{plantId}/day` if it
returns all four energy series (pv, battery, grid, load) in one call — verify
this from the library source. If it returns usable daily totals, prefer it.

**Fallback approach:** Call the individual `/day` endpoints
(`/api/v1/inverter/{sn}/day`, `/api/v1/inverter/grid/{sn}/day`,
`/api/v1/inverter/battery/{sn}/day`, `/api/v1/inverter/load/{sn}/day`) and
**integrate the time-series records** (sum `value * Δt` over all records
for the day) to compute daily kWh totals. Do not rely on any scalar field
unless confirmed present in actual responses.

**Per-half-hour series:** store individual records from the battery SoC
series into `solar_intervals` for intraday charting and accurate peak-SoC
calculation.

### Environment variables

| Variable | Description |
|---|---|
| `SUNSYNK_USERNAME` | sunsynk.net login email |
| `SUNSYNK_PASSWORD` | sunsynk.net password |
| `SUNSYNK_INVERTER_SN` | Inverter serial (from Sunsynk Connect → Equipment) |
| `SUNSYNK_PLANT_ID` | Plant ID (from URL when viewing plant on sunsynk.net) |

---

## Octopus Energy client (`octopus.py`)

### Authentication

HTTP Basic auth: API key as username, empty password. Key from
`https://octopus.energy/dashboard/developer/`.

Base URL: `https://api.octopus.energy/v1`

### Meter and tariff discovery

`GET /accounts/{account_number}/` returns a full account structure. Parse:
- `properties[0].electricity_meter_points[]` for import and export MPANs
- `meters[0].serial_number` for each MPAN
- `agreements[]` for current tariff code — filter to the active agreement
  where `valid_from <= now` and `(valid_to is null or valid_to > now)`.
  Do not just take `sorted()[-1]` as that can select a future-scheduled tariff.

The Cosy tariff code is `E-1R-COSY-22-12-08-{region}`. The Outgoing Octopus
export tariff code follows the same pattern. The gas tariff code is similarly
structured. Extract the region letter (final character) — needed for rate
lookups.

### Electricity import consumption

```
GET /electricity-meter-points/{mpan}/meters/{serial}/consumption/
    ?period_from={date}T00:00:00{offset}
    &period_to={next_date}T00:00:00{offset}
    &page_size=100
    &order_by=period
```

Returns 48 half-hourly intervals for one day (or fewer if data is missing).
Parse `interval_start` and `interval_end` as offset-aware datetimes, convert
to `Europe/London` timezone before Cosy window classification.

Flag as partial if `len(results) < 40`. Write a `sync_log` record with
`status='partial'` and `message='N/48 intervals'`.

### Electricity export consumption

Same endpoint pattern but using the **export MPAN and its own serial number**
(not the import serial). The export MPAN has a different serial — they are
independent meters. Auto-discover both from the account endpoint.

### Gas consumption

```
GET /gas-meter-points/{mprn}/meters/{serial}/consumption/
    ?period_from=…&period_to=…&page_size=100&order_by=period
```

Same half-hourly shape. Note gas consumption from the smart meter is in
**m³** not kWh — convert using a calorific value of **39.5 MJ/m³** and a
correction factor of **1.02264**, giving:

```
kwh = m3 * 1.02264 * 39.5 / 3.6
```

(This is the standard formula used on UK gas bills and matches Octopus's
own conversion.)

### Cosy tariff rates

Product code: `COSY-22-12-08`
Tariff code: `E-1R-COSY-22-12-08-{region}`

```
GET /products/COSY-22-12-08/electricity-tariffs/{tariff_code}/standard-unit-rates/
    ?period_from=…&period_to=…
```

**Cosy window classification — use time-of-day, not value rank:**

```python
def cosy_window(ts_local: datetime) -> str:
    h = ts_local.hour + ts_local.minute / 60.0
    if 4 <= h < 7 or 13 <= h < 16 or 22 <= h < 24:
        return "cheap"
    if 16 <= h < 19:
        return "peak"
    return "day"
```

Do NOT classify by sorting `value_inc_vat` and assigning by rank. That
breaks if a quarterly price-cap change produces >3 distinct values in the
window, or if two bands happen to equal each other.

### Gas rates

```
GET /v1/gas-meter-points/{mprn}/meters/{gas_serial}/
GET /products/{gas_product}/gas-tariffs/{gas_tariff}/standard-unit-rates/
GET /products/{gas_product}/gas-tariffs/{gas_tariff}/standing-charges/
```

Auto-discover the gas product and tariff code from the account endpoint
(`properties[0].gas_meter_points[0].agreements[]`).

### Standing charges

Fetch from the `/standing-charges/` endpoint for each tariff. Store p/day
in `tariff_rates` and add to daily summaries. Standing charge is added once
per day, not per interval.

### Flexible Octopus rate (for savings comparison)

To calculate "saving vs standard tariff", fetch the current Flexible Octopus
unit rate for the same region:

```
GET /products/?brand=OCTOPUS_ENERGY&is_variable=true
```

Find the product with `display_name == "Flexible Octopus"` and
`direction == "IMPORT"`, then fetch its unit rate for the region. Use this
as the counterfactual flat rate. Label the saving clearly in the UI and brief
as "estimated saving vs Flexible (unshifted)".

### Rate limits

No documented hard limit. Back off on 429 with exponential retry. Always
set a descriptive `User-Agent` header (e.g. `PowerSOC/1.0`).

### Environment variables

| Variable | Description |
|---|---|
| `OCTOPUS_API_KEY` | From octopus.energy/dashboard/developer/ |
| `OCTOPUS_ACCOUNT_NUMBER` | Format: A-XXXXXXXX |
| `OCTOPUS_MPAN` | Import MPAN (auto-discovered if blank) |
| `OCTOPUS_METER_SERIAL` | Import meter serial (auto-discovered if blank) |
| `OCTOPUS_EXPORT_MPAN` | Export MPAN (auto-discovered if blank) |
| `OCTOPUS_EXPORT_SERIAL` | Export meter serial (auto-discovered if blank) |
| `OCTOPUS_MPRN` | Gas MPRN (auto-discovered if blank) |
| `OCTOPUS_GAS_SERIAL` | Gas meter serial (auto-discovered if blank) |

---

## Calculations

### Self-sufficiency

Use the robust, billing-consistent formula:

```
self_sufficiency_pct = (1 - grid_import_kwh / house_load_kwh) * 100
```

Clamp to [0, 100]. This uses only grid_import (Octopus metered, authoritative)
and house_load (Sunsynk). Note in CLAUDE.md that grid_import from Octopus
and grid_import from Sunsynk may not reconcile (CT clamp vs settlement meter)
— Octopus is authoritative for billing; Sunsynk is used for energy-flow
breakdown only.

### Net cost

```
net_cost_pence = import_cost_pence + standing_elec_pence + gas_cost_pence
               + standing_gas_pence - export_earned_pence
```

The dashboard should show electricity-only, gas-only, and combined totals
with a toggle. Export earnings are shown as a credit line, not netted from
import in the detailed breakdown.

### Estimated saving vs Flexible

```
saving_pence = (flex_rate_p * total_import_kwh) - import_cost_pence
```

Label as: "Estimated saving on import vs Flexible Octopus (unshifted load)"
— not a true counterfactual since on Flexible you might not time-shift load.

---

## Sync and backfill (`sync.py`)

### First-run deep backfill

On first run (empty database), attempt to fetch all available history from
both APIs. This is a background task — do not block the web server startup.
Show a "backfill in progress" banner on the dashboard until complete.

- **Octopus:** Request in 90-day windows from account registration date to
  today. The API supports arbitrary date ranges. Paginate with `page_size=100`
  (max 1500). For years of data this will take several minutes — log progress.
- **Sunsynk:** Request day-by-day going back in time until the API returns
  empty results (no data available). Do not go back more than 2 years. The
  cloud API's historical depth is not guaranteed — stop gracefully when data
  runs out.
- Write all fetched intervals to `solar_intervals`,
  `electricity_intervals`, `export_intervals`, `gas_intervals`.
- After inserting intervals for each day, compute and upsert that day's
  `daily_summaries` row.
- Write a `sync_log` record for each day fetched.

### Daily incremental sync

Fetch yesterday's data at ~06:25 UK local time. Schedule this via:
1. **Claude Code routine** (preferred) — implement as a Claude Code
   scheduled task if the environment supports it.
2. **Fallback cron:** `/etc/cron.d/powersoc` running as the service user:
   ```
   TZ=Europe/London
   25 6 * * * powersoc /opt/powersoc/venv/bin/python /opt/powersoc/sync_daily.py
   ```

`sync_daily.py` is a thin CLI wrapper around `sync.run_daily()` that:
1. Fetches yesterday's Sunsynk data
2. Fetches yesterday's Octopus electricity import + export
3. Fetches yesterday's Octopus gas consumption
4. Upserts intervals into the DB
5. Computes/updates yesterday's `daily_summaries` row
6. Posts the morning brief to Mattermost
7. Logs result to `sync_log`

### Rate refresh

Refresh `tariff_rates` weekly (Sunday 00:00) — Cosy and gas rates change
quarterly but it is cheap to poll weekly and ensures the dashboard is never
stale after a price cap adjustment.

---

## Dashboard (`app.py`, `templates/`, `static/`)

### Pages

**`/` → dashboard (today)**
The main view. Shows:
- Summary card row (today's headline figures)
- Intraday charts (one per metric, Chart.js v4)
- Date navigator (← previous day | today | next day →)

**`/history` → historical view**
Day / Week / Month / Year tabs. Each tab shows:
- A chart (type varies by timeframe — see below)
- A summary table below the chart
- Electricity / Gas / Both toggle for cost totals

**`/settings` → configuration UI**
- API credentials (Sunsynk, Octopus)
- Meter/tariff auto-discovery button ("Discover meters")
- Notification settings (Mattermost webhook URL, channel)
- Tariff overrides (manual rate entry if auto-discovery fails)
- Backfill controls (trigger manual re-backfill)
- User management (add/disable/reset password)
- Theme preference

**`/widget/today` → embeddable summary (no auth)**
A minimal, iframe-safe page showing today's headline numbers. No navigation,
no chrome. Designed to drop into Homarr, Homer, or any dashboard that
supports iframe widgets. Serve with `X-Frame-Options: SAMEORIGIN` relaxed
to `ALLOWALL` on this route only (or make it configurable).

### Intraday charts (today view)

One Chart.js chart per metric. All use half-hourly (or hourly) time axis.

| Chart | Type | Series |
|---|---|---|
| Energy flow | Stacked area | Solar PV, Battery discharge, Grid import |
| Battery SoC | Line | SoC % over the day |
| House consumption | Bar | kWh per half-hour |
| Import cost | Bar (stacked) | Cheap / Day / Peak windows colour-coded |
| Export | Bar | Export kWh per half-hour |
| Gas consumption | Bar | kWh per half-hour |

### Historical charts (history view)

| Timeframe | Chart type | X-axis |
|---|---|---|
| Week | Grouped bar | Day of week (Mon–Sun) |
| Month | Bar | Day of month |
| Year | Bar | Month (Jan–Dec) |

All historical charts support the Electricity / Gas / Both cost toggle.
"Both" stacks electricity and gas as two series.

### Theme

Two themes: `dark` and `light`, defined as CSS custom properties in
`themes.css`. Toggle button in the nav bar. Persist preference in
`localStorage` (client-side, no server round-trip). Default to dark.

Chart.js charts must re-theme on toggle — listen for the theme change and
call `chart.update()` with updated colour options.

### Auth

PBKDF2-SHA256, 200k iterations, same scheme as SOC dashboard:
`pbkdf2$iters$salt_b64$hash_b64`.

Flask session cookie: HttpOnly, SameSite=Lax, 30-day lifetime. `Secure`
flag enabled when `POWERSOC_COOKIE_SECURE=1`.

Every endpoint except `/login`, `/setup`, `/widget/*`, and `/static/*`
requires a valid session. API endpoints return 401 JSON on missing auth (no
redirect). First run with empty `users` table redirects to `/setup`.

### API shape

Every `/api/*` endpoint returns `{success: bool, data: …, error: str|null}`.
Use `ok(data)` and `err(msg, code)` helpers.

---

## Notifications (`notifications.py`, `brief.py`)

### Mattermost webhook

Configure in settings UI: webhook URL (stored Fernet-encrypted in `settings`
table), channel override (default: `#energy`), username (default: `PowerSOC`),
icon emoji (default: `:zap:`).

### Daily morning brief

Built by `brief.py`. Posts after the daily sync completes (~06:25). Always
posts — if data is missing, posts a degraded message with `⚠️` warnings.

**Message format:**

```
### ⚡ PowerSOC — Energy Brief · Mon 26 May 2026

**☀️ Solar & Battery**
- Generated: **18.4 kWh** · Exported: **6.8 kWh**
- Battery: peak **100%** · end-of-day **62%**
- Discharge: **9.2 kWh** · Charge: **11.8 kWh**

**🔌 Electricity (Cosy Octopus)**
- Import: **4.3 kWh** (cheap 3.1 / day 0.9 / peak 0.3)
- Import cost: **£0.42** · Standing: **£0.55**
- Export earned: **£0.27** (6.8 kWh × 4.1p)
- Est. saving vs Flexible: **£0.74**

**🔥 Gas**
- Used: **12.4 kWh** (1.23 m³)
- Cost: **£0.88** · Standing: **£0.32**

**📊 Day total**
- Net electricity cost: **£0.70** (after export credit)
- Total inc. gas: **£1.90**
- Self-sufficiency: **86%** 🌞

**Notable**
- 🔋 Battery hit 100% SoC
- 🌞 Net exporter — exported more than imported
```

If Sunsynk data is unavailable, replace solar/battery section with:
`☀️ Solar & Battery — ⚠️ Sunsynk data unavailable`

If Octopus data is unavailable or partial, append interval count:
`⚠️ Octopus data partial (38/48 intervals — may update later)`

### Notable event detection

Flag in the brief if any of:
- Battery peak SoC ≥ 99.5% ("Battery hit 100%")
- Export kWh > import kWh ("Net exporter day")
- Peak-window import > 2 kWh ("High peak usage — check schedule")
- Battery end-of-day SoC < 10% ("Battery nearly depleted")
- Gas usage > 150% of 30-day average ("High gas day")
- Any API fetch returned 0 intervals ("No data from [source]")

---

## Setup wizard (`/setup`)

Multi-step wizard on first run:

1. **Create admin account** — username + password
2. **Sunsynk credentials** — username, password, inverter serial, plant ID
   — "Test connection" button that attempts login and lists plants
3. **Octopus credentials** — API key, account number
   — "Discover meters" button that hits `/accounts/` and auto-populates
   MPAN, serial, export MPAN, export serial, MPRN, gas serial, tariff codes
4. **Notification settings** — Mattermost webhook URL, channel
   — "Test webhook" button
5. **Review & start backfill** — shows what will be fetched, "Start" button
   triggers background backfill and redirects to dashboard with progress banner

All values entered in the wizard are persisted to the `settings` table
(credentials Fernet-encrypted with a key derived from `/etc/machine-id`).
They are also exportable as a `.env` file snippet from the settings page
(for users who prefer env-var deployment).

---

## Systemd service

`powersoc.service`:
```ini
[Unit]
Description=PowerSOC Energy Dashboard
After=network.target

[Service]
Type=simple
User=powersoc
WorkingDirectory=/opt/powersoc
EnvironmentFile=/opt/powersoc/.env
ExecStart=/opt/powersoc/venv/bin/python app.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

`deploy.sh` must:
1. Create `/opt/powersoc/` and service user `powersoc` if missing
2. Rsync source (excluding venv/, data/, .env)
3. Create venv + pip install -r requirements.txt
4. Install systemd unit + daemon-reload
5. Start/restart service
6. Tail logs for 10 seconds
7. Print the URL and the `/setup` path

---

## `CLAUDE.md` (committed to repo)

Write a comprehensive `CLAUDE.md` that documents:
- Project purpose and architecture overview
- All known API gotchas (list below — expand as you discover more)
- Database schema rationale
- Conventions (JSON API shape, DB connection pattern, error handling)
- How to extend (add a chart, add a metric, add a notification platform)
- Deployment and recovery procedures
- Open items / roadmap

**Known API gotchas to document:**

1. **Sunsynk /day endpoints return time-series, not scalars.** Daily kWh totals
   must be integrated from the series or sourced from the plant energy endpoint.
   Verify which approach works against live API before trusting numbers.
2. **Sunsynk token re-login.** Token lasts ~6 days. On 401, clear cache and
   re-login once. Log the event.
3. **Sunsynk platform outages.** 502s during region migrations are common.
   Always degrade gracefully.
4. **Octopus data lag.** SMETS2 data may not be available until 9am or later
   (~50% of days). Sometimes not until the following day. Never fail the brief
   because of missing intervals — mark as partial and post anyway.
5. **Gas is in m³, not kWh.** Convert with: `kwh = m3 * 1.02264 * 39.5 / 3.6`
6. **Cosy windows are local time, not UTC.** Always convert to
   `Europe/London` before classifying intervals. DST transitions produce
   47 or 49 intervals on the transition day — handle gracefully.
7. **Active tariff selection.** Filter agreements to `valid_from <= now AND
   (valid_to IS NULL OR valid_to > now)`. Do not just take the most recent
   by date — a future-dated agreement may be scheduled.
8. **Export MPAN has its own serial.** Do not reuse the import serial against
   the export MPAN — it will 404. Discover both independently.
9. **Cosy rate classification by time, not by value rank.** Quarterly
   price-cap changes can produce >3 distinct rate values in a 24h window,
   breaking a rank-based classifier.
10. **Standing charges are p/day, not p/kWh.** Add once per daily summary.
11. **Saving vs Flexible is approximate.** It assumes the same consumption
    pattern on Flexible (no load-shifting). Label accordingly in UI and brief.
12. **Sunsynk inverter clock drift.** If the inverter clock isn't set to
    BST/GMT, day boundaries may be wrong. Document how to check in README.

---

## README.md

Must cover:
- What PowerSOC is (one paragraph, no jargon)
- Screenshots or ASCII mockup of the dashboard
- Prerequisites (Python 3.11+, Sunsynk Connect account, Octopus Energy
  account with developer API access enabled)
- Quick start (clone → deploy.sh → /setup)
- Environment variables reference table
- Supported inverters (Sunsynk hybrid range via sunsynk.net)
- Supported tariffs (Cosy Octopus import, Outgoing Octopus export, Octopus
  gas — adaptable to other Octopus tariffs)
- Widget embedding guide (iframe snippet for Homarr/Homer)
- Contributing / forking notes
- Licence (MIT)

---

## `.env.example`

```bash
# PowerSOC environment configuration
# Copy to .env and fill in your values

# --- Sunsynk ---
SUNSYNK_USERNAME=        # Your sunsynk.net login email
SUNSYNK_PASSWORD=        # Your sunsynk.net password
SUNSYNK_INVERTER_SN=     # Inverter serial (Sunsynk Connect → Equipment → Inverter)
SUNSYNK_PLANT_ID=        # Plant ID (visible in URL when viewing your plant)

# --- Octopus Energy ---
OCTOPUS_API_KEY=         # From https://octopus.energy/dashboard/developer/
OCTOPUS_ACCOUNT_NUMBER=  # Format: A-XXXXXXXX (shown on bills / dashboard)
# The following are auto-discovered from the account API if left blank:
OCTOPUS_MPAN=
OCTOPUS_METER_SERIAL=
OCTOPUS_EXPORT_MPAN=
OCTOPUS_EXPORT_SERIAL=
OCTOPUS_MPRN=
OCTOPUS_GAS_SERIAL=

# --- Notifications ---
MATTERMOST_WEBHOOK_URL=  # Incoming webhook URL from your Mattermost instance
MATTERMOST_CHANNEL=      # Default: #energy

# --- Application ---
POWERSOC_PORT=8073
POWERSOC_DATA_DIR=/opt/powersoc/data
POWERSOC_COOKIE_SECURE=0   # Set to 1 when behind HTTPS reverse proxy
LOG_LEVEL=INFO
TZ=Europe/London
```

---

## Build order

Build in this sequence — each phase should be committed and testable:

**Phase 1 — Foundation**
- Initialise git, `.gitignore`, `.env.example`
- `database.py` — schema, migrations, `db.conn()` context manager
- `config.py` — env loading, settings dataclass
- `auth.py` — PBKDF2, session middleware, `/login`, `/setup` wizard stub
- `app.py` — Flask skeleton, `ok()`/`err()` helpers, static file serving
- `templates/base.html`, `themes.css`, `main.css` stub
- Smoke test: `python app.py` → `/setup` loads

**Phase 2 — Sunsynk client**
- `sunsynk.py` — login, token cache, retry wrapper, `fetch_day()`
- Verify paths from installed library source before writing
- Unit tests in `tests/test_sunsynk.py` for series integration logic
- Manual test: `python -c "from sunsynk import SunsynkClient; ..."`

**Phase 3 — Octopus client**
- `octopus.py` — auth, account/meter discovery, consumption, rates
- `tests/test_octopus.py` — `cosy_window()` DST boundaries, gas m³→kWh,
  tariff agreement selection, rate classification
- Manual test against live API (read-only, safe)

**Phase 4 — Sync and backfill**
- `sync.py` — `run_daily()`, `run_backfill()`, `sync_log` writes
- `sync_daily.py` — CLI entry point
- Background backfill thread started by `app.py` on first run
- Progress endpoint `GET /api/backfill/status` → `{running, days_done, days_total}`

**Phase 5 — Dashboard and charts**
- `templates/dashboard.html` — summary cards + intraday chart placeholders
- `static/js/main.js` — Chart.js initialisation, half-hourly data fetch
- `GET /api/today` → returns all intraday series for today
- `GET /api/day/{date}` → returns all series for a given date
- Date navigator (← / today / →)

**Phase 6 — History view**
- `templates/history.html` — D/W/M/Y tabs
- `GET /api/history?period=week|month|year&anchor=YYYY-MM-DD`
- Electricity / Gas / Both toggle

**Phase 7 — Notifications**
- `brief.py` — daily summary builder, notable event detection
- `notifications.py` — Mattermost formatter, delivery, retry
- Integrate into `sync_daily.py`
- Test with `POST /api/brief/test` → sends a synthetic brief

**Phase 8 — Settings UI and widget**
- `templates/settings.html` — credentials, discovery, notifications, users
- `GET/POST /api/settings/*` routes
- `widgets.py` — `/widget/today` embeddable page
- Fernet encryption for stored credentials

**Phase 9 — Deploy and docs**
- `deploy.sh` — working end-to-end
- `powersoc.service`
- `README.md` — complete, fork-friendly
- `CLAUDE.md` — complete, with all gotchas documented
- Final `requirements.txt`

---

## Tests

Write tests before or alongside each module. Minimum coverage:

- `tests/test_octopus.py`: `cosy_window()` for all three windows + DST
  boundary; gas m³→kWh conversion; active tariff selection (current, future,
  expired); partial interval handling; export MPAN serial independence
- `tests/test_sunsynk.py`: series integration (sum of records × Δt);
  peak SoC extraction; graceful handling of empty records
- `tests/test_brief.py`: self-sufficiency formula; net cost calculation;
  notable event detection; degraded brief (Sunsynk unavailable, Octopus
  partial)
- `tests/test_calculations.py`: saving vs Flexible; standing charge
  addition; Cosy window breakdown

Use `pytest`. No external API calls in tests — mock all HTTP.

---

## Definition of done

The build is complete when:

1. `python app.py` starts on a fresh Ubuntu host with only `.env` configured
2. `/setup` wizard discovers Octopus meters automatically and tests Sunsynk
   login successfully
3. Background backfill runs and populates `daily_summaries` with historical data
4. Dashboard shows today's intraday charts with correct data
5. History view shows W/M/Y summaries with Electricity/Gas/Both toggle
6. Dark/light theme toggle works and chart colours update
7. `python sync_daily.py` runs without error and posts to Mattermost
8. `/widget/today` loads without auth and is embeddable in an iframe
9. `deploy.sh` runs end-to-end on a clean Ubuntu host
10. `pytest` passes with no external API calls
11. No hardcoded hostnames, account numbers, or credentials anywhere in source
12. README is written for a stranger, not for this homelab
