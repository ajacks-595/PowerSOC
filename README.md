# ⚡ PowerSOC

PowerSOC is a self-hosted home-energy dashboard and daily briefing tool. It
pulls data from a **Sunsynk** hybrid solar inverter (via the Sunsynk Connect
cloud API) and **Octopus Energy** (electricity import, export, and gas), stores
everything locally in SQLite, serves a clean Flask web dashboard, and posts a
morning energy summary to **Mattermost**. No cloud services, no CDNs — it runs
entirely on your own hardware and works air-gapped.

> **Unofficial project.** PowerSOC is independent and is not affiliated with,
> endorsed by, or supported by Sunsynk, Octopus Energy, or Mattermost. See
> [Disclaimer](#disclaimer).

---

## Screenshot (ASCII mockup)

```
 ⚡ PowerSOC      Dashboard | History | Settings              admin  ◑  Log out
 ┌──────────────────────────────────────────────────────────────────────────┐
 │ Today                                       ←  [ 2026-05-25 ]  Today  →    │
 ├────────────┬────────────┬────────────┬────────────┬───────────┬──────────┤
 │ SOLAR      │ SELF-SUFF. │ NET COST   │ BATTERY    │ IMPORT    │ GAS      │
 │ 18.4 kWh   │ 86%        │ £1.90      │ 100% peak  │ 4.3 kWh   │ 12.4 kWh │
 │ exp 6.8kWh │            │ after exp. │ end 62%    │ c/d/p     │ £0.88    │
 ├────────────┴────────────┴────────────┴────────────┴───────────┴──────────┤
 │  [ Energy flow ]      [ Battery SoC ]      [ House consumption ]          │
 │  [ Import cost  ]     [ Export       ]     [ Gas consumption   ]          │
 └──────────────────────────────────────────────────────────────────────────┘
```

---

## Prerequisites

- **Python 3.11+**
- A **Sunsynk Connect** account (the app/website you use to view your inverter)
- An **Octopus Energy** account with **API access enabled**
  (get your key at <https://octopus.energy/dashboard/developer/>)
- *(optional)* A Mattermost incoming webhook for the daily brief
- A Linux host for deployment (tested on **Ubuntu 22.04 / 24.04 LTS**)

---

## Quick start

### Docker (recommended)

```bash
git clone <your-fork-url> powersoc
cd powersoc
mkdir -p host/backups                                   # backups live HERE,
head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n' > host/machine_id   # + key seed
echo "POWERSOC_MACHINE_ID_FILE=/host/machine_id" > .env  # compose interpolation
sudo chown -R 10001:10001 host
docker compose up -d --build

# Read the one-time setup token, then open the first-run wizard:
docker logs powersoc 2>&1 | grep "setup token"
#   http://<host>:8073/setup
```

Or, from a workstation, `DEPLOY_HOST=user@host scripts/deploy-docker.sh` does
all of the above over SSH (and `RESTORE_FROM=/path/on/host.db` restores a
backup before the first start). **Paths:** the manual steps above keep backups
and the key seed in `./host` beside `docker-compose.yml`; the deploy script
uses `~/powersoc-host` on the target instead (override with `HOST_DIR=`). Same
layout either way — `POWERSOC_HOST_DIR` in the compose `.env` decides.

The container runs as a non-root user (uid 10001) with the in-app scheduler
enabled (daily 06:25 sync + brief, hourly intraday refresh, nightly DB backup).
The SQLite DB lives in the named volume `powersoc-data` (`/data`); **backups
and the encryption key seed live in the host directory `host/`** (bind-mounted
at `/host`, override with `POWERSOC_HOST_DIR`) — deliberately *outside* the
volume, so losing the volume does not take the backups with it.

### Backups, restore & monitoring

- **Backups:** nightly after the 06:25 run, written to `POWERSOC_BACKUP_DIR`
  (`/host/backups` in Docker), verified with `PRAGMA quick_check`, mode 0600,
  14 kept. `/api/health` reports `backup_ok`, `/metrics` exports
  `powersoc_last_backup_age_seconds`. For protection against losing the whole
  host, sync that directory off-box (rsync/restic from another machine).
- **Key seed:** keep a copy of `host/machine_id` somewhere safe (password
  manager). Backups contain Fernet-encrypted credentials that only decrypt with
  the same seed; without it you re-enter credentials (users/history survive).
- **Restore:** stop the app, then
  `docker compose run --rm --no-deps --entrypoint python powersoc app.py --restore /host/backups/powersoc-YYYY-MM-DD.db`
  (host install: `venv/bin/python app.py --restore <file>`). The current DB is
  moved aside, never deleted; older backups are migrated forward.
- **Monitoring:** set `POWERSOC_HEARTBEAT_URL` to a healthchecks.io check or an
  Uptime Kuma *push* monitor — PowerSOC pings it after every hourly tick and the
  daily run (`/fail` appended on failure), and the monitor alerts when pings
  **stop**, which is the only way to notice the box itself being down.
  Alternatively point an HTTP monitor at `/api/health` and alert on
  `scheduler_ok` / `backup_ok` being false.

### Host install (systemd + cron)

```bash
git clone <your-fork-url> powersoc
cd powersoc

# Deploy on the target host (creates the powersoc user, venv, systemd unit
# running gunicorn, the daily-sync cron job, and a logrotate policy):
sudo ./deploy.sh
# Point backups at a different disk: POWERSOC_BACKUP_DIR=/mnt/backup/powersoc
# in /opt/powersoc/.env (the cron job writes them after the 06:25 run).

# Then open the first-run wizard and add your credentials:
#   http://<host>:8073/setup
```

> Cron fires in the **host's** timezone — keep the host on `Europe/London`
> (`timedatectl set-timezone Europe/London`) or the 06:25 brief drifts an hour
> during BST. The Docker path is unaffected.

You can also run it directly for development:

```bash
python3 -m venv venv && . venv/bin/activate
pip install -r requirements.txt
python app.py                 # http://localhost:8073 — then open /setup
```

On first run, `/setup` asks for a **one-time setup token** printed in the
server log (`grep "setup token"`) — so only someone with access to the host can
claim the admin account — then creates your admin account and drops you on the Settings
page where you enter your Sunsynk and Octopus credentials, **Discover meters**
(auto-fills MPANs/serials/tariffs from your account), test the connections, and
**Start backfill** to pull your history.

---

## Configuration

**You configure PowerSOC entirely in the browser.** The first-run `/setup`
wizard walks you through your admin account, Sunsynk and Octopus credentials
(with **Discover meters** to auto-fill MPANs/serials/tariffs and **Test**
buttons), and the Mattermost brief. Everything is editable later under
**Settings**. Credentials (and 2FA secrets) are stored **Fernet-encrypted** in
SQLite, keyed to `POWERSOC_MACHINE_ID(_FILE)` or the host's `/etc/machine-id` —
they never touch a config file. No `.env` is required.

<details>
<summary><strong>Optional: environment variables</strong> (for unattended /
IaC deploys)</summary>

If you'd rather provision without the UI, create a `.env` (the systemd unit
sources it if present) — see `.env.example` for the annotated template. UI
values always take precedence over env vars. Credentials:
`SUNSYNK_USERNAME`, `SUNSYNK_PASSWORD`, `SUNSYNK_INVERTER_SN`,
`SUNSYNK_PLANT_ID`, `OCTOPUS_API_KEY`, `OCTOPUS_ACCOUNT_NUMBER` (the meter
MPANs/serials auto-discover), `MATTERMOST_WEBHOOK_URL`, `MATTERMOST_CHANNEL`.
App settings: `POWERSOC_PORT` (8073), `POWERSOC_DATA_DIR`,
`POWERSOC_BACKUP_DIR`, `POWERSOC_MACHINE_ID_FILE`, `POWERSOC_HEARTBEAT_URL`,
`POWERSOC_SETUP_TOKEN`, `POWERSOC_COOKIE_SECURE` (1 behind HTTPS),
`POWERSOC_FERNET_SALT`, `TZ`. In `.env`, put comments on their own line; a
blank value means "use the default".

</details>

---

## Supported hardware & tariffs

- **Inverters:** Sunsynk hybrid range, via sunsynk.net (Sunsynk Connect cloud).
- **Tariffs:** Cosy Octopus (import), Outgoing Octopus (export), and Octopus
  gas. The code discovers your tariff codes from the account API, so other
  Octopus tariffs largely work too — the Cosy time-of-use *window* colouring is
  specific to Cosy and can be adapted in `octopus.cosy_window()`.

---

## Embedding the widget

`GET /widget/today` is a no-auth, chrome-free summary page designed to drop into
another dashboard (Homarr, Homer, a custom page) via an iframe:

```html
<iframe src="http://<host>:8073/widget/today?theme=dark"
        style="width:340px;height:260px;border:0"></iframe>
```

It refreshes itself every 5 minutes and accepts `?theme=dark|light`.

---

## Contributing / forking

PowerSOC is generic-first: there are no hardcoded hostnames, account numbers, or
credentials anywhere in the source. Fork it, run `deploy.sh`, and configure
through the wizard. Tests run with `pytest` and never touch the network
(all HTTP is mocked):

```bash
. venv/bin/activate
pytest -q
```

See `CLAUDE.md` for architecture, API gotchas, and extension notes.

---

## Disclaimer

PowerSOC is an independent, unofficial project. It is **not affiliated with,
endorsed by, or supported by** Sunsynk, Octopus Energy, Mattermost, the
National Energy System Operator (NESO), or Open-Meteo. Product and company
names are trademarks of their respective owners and are used only to describe
compatibility.

- **Unofficial APIs.** The Sunsynk integration talks to the Sunsynk Connect
  cloud API, which is undocumented and can change or break without notice. The
  Octopus integration uses Octopus Energy's customer REST and GraphQL APIs with
  your own API key. Your use of these services remains subject to their terms.
- **Read-only.** Apart from logging in, PowerSOC only reads data — it never
  sends commands to your inverter or changes anything on your energy account.
- **Not a bill.** Costs, savings, and forecasts are estimates calculated from
  what the APIs return; your supplier's bill is authoritative.

The software is provided "as is", without warranty of any kind — see `LICENSE`.

---

## Licence

MIT — see `LICENSE`.
