#!/usr/bin/env bash
#
# Deploy PowerSOC to an Ubuntu 22.04+/24.04 host.
#
# Run this ON the target host as a user with sudo (the simplest path for a
# fresh fork):
#
#     sudo ./deploy.sh
#
# or, to deploy to a remote host from your workstation:
#
#     DEPLOY_HOST=user@host ./deploy.sh
#
# Configurable via env vars:
#   INSTALL_DIR   (default /opt/powersoc)
#   SERVICE_USER  (default powersoc)
#   DEPLOY_HOST   (optional user@host for remote deploy over SSH)

set -euo pipefail
cd "$(dirname "$0")"

INSTALL_DIR="${INSTALL_DIR:-/opt/powersoc}"
SERVICE_USER="${SERVICE_USER:-powersoc}"
DEPLOY_HOST="${DEPLOY_HOST:-}"
PORT="${POWERSOC_PORT:-8073}"

say()  { printf "\n\033[1;36m>>> %s\033[0m\n" "$*"; }
warn() { printf "\033[1;33m!! %s\033[0m\n" "$*"; }
err()  { printf "\033[1;31mXX %s\033[0m\n" "$*" >&2; }

# ---- remote mode: rsync this tree to the host and re-run there ----
# Optional: SSH_KEY=~/.ssh/id_ed25519   SSH_PORT=22
if [ -n "$DEPLOY_HOST" ]; then
  SSH_KEY="${SSH_KEY:-}"
  SSH_PORT="${SSH_PORT:-22}"
  # Array (not a string) so a key path containing spaces survives quoting.
  SSH_OPTS=(-o StrictHostKeyChecking=accept-new -p "$SSH_PORT")
  [ -n "$SSH_KEY" ] && SSH_OPTS+=(-i "$SSH_KEY")
  say "Remote deploy to $DEPLOY_HOST (key: ${SSH_KEY:-default}, port $SSH_PORT)"

  # Stage in a per-run unpredictable directory: a fixed world-writable
  # /tmp/powersoc-deploy/ could be pre-created by any local user on the target
  # and its contents swapped between our rsync and the root install below.
  STAGE="$(ssh "${SSH_OPTS[@]}" "$DEPLOY_HOST" 'mktemp -d /tmp/powersoc-deploy.XXXXXXXX')"
  say "Staging in $STAGE"

  rsync -az --delete \
    --exclude 'venv/' --exclude 'data/' --exclude '.env' \
    --exclude '__pycache__/' --exclude '*.log' --exclude '.git/' \
    --exclude '.repo-review/' \
    -e "ssh ${SSH_OPTS[*]}" \
    ./ "$DEPLOY_HOST:$STAGE/"

  # Install on the target as root. `sudo env VAR=...` reliably passes the
  # install vars through sudo (no SETENV sudoers rule required). The inner
  # \$VARS expand remotely; the literal values were expanded locally.
  # shellcheck disable=SC2029
  ssh -t "${SSH_OPTS[@]}" "$DEPLOY_HOST" \
    "sudo env INSTALL_DIR='$INSTALL_DIR' SERVICE_USER='$SERVICE_USER' POWERSOC_PORT='$PORT' \
       sh -c 'mkdir -p \"\$INSTALL_DIR\" && \
         rsync -a --delete --exclude venv/ --exclude data/ --exclude .env \
           '$STAGE'/ \"\$INSTALL_DIR/\" && \
         cd \"\$INSTALL_DIR\" && ./deploy.sh'; rm -rf '$STAGE'"
  exit 0
fi

# ---- local mode (must be root) ----
if [ "$(id -u)" != "0" ]; then
  err "Run with sudo on the target host:  sudo ./deploy.sh"
  exit 1
fi

# Preflight: fail early with a clear message instead of mid-run.
command -v rsync >/dev/null || { err "rsync is required (apt install rsync)"; exit 1; }
python3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" \
  || { err "Python >= 3.11 required"; exit 1; }
python3 -m venv --help >/dev/null 2>&1 \
  || { err "python3-venv is required (apt install python3-venv)"; exit 1; }
case "$INSTALL_DIR" in /home/*)
  warn "INSTALL_DIR under /home conflicts with ProtectHome=true in the unit";;
esac
if [ "$(timedatectl show -p Timezone --value 2>/dev/null)" != "Europe/London" ]; then
  warn "Host timezone is not Europe/London — cron fires in HOST time, so the"
  warn "06:25 brief will drift over BST. Fix: timedatectl set-timezone Europe/London"
fi

say "Create service user '$SERVICE_USER' if missing"
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
  useradd --system --create-home --shell /usr/sbin/nologin "$SERVICE_USER"
fi

say "Create $INSTALL_DIR"
mkdir -p "$INSTALL_DIR/data"

# If we are not already running from INSTALL_DIR, sync this tree into it.
SRC="$(pwd)"
if [ "$SRC" != "$INSTALL_DIR" ]; then
  say "Sync source → $INSTALL_DIR (excluding venv/ data/ .env)"
  rsync -a --delete \
    --exclude 'venv/' --exclude 'data/' --exclude '.env' \
    --exclude '__pycache__/' --exclude '*.log' --exclude '.git/' \
    --exclude '.repo-review/' \
    "$SRC"/ "$INSTALL_DIR"/
fi

say "Ensure the backup directory exists (outside $INSTALL_DIR)"
mkdir -p /var/backups/powersoc
chown "$SERVICE_USER":"$SERVICE_USER" /var/backups/powersoc
chmod 0700 /var/backups/powersoc

say "Ensure log files exist"
touch /var/log/powersoc.log /var/log/powersoc-sync.log
chown "$SERVICE_USER":"$SERVICE_USER" /var/log/powersoc.log /var/log/powersoc-sync.log

say "Create / refresh virtualenv and install dependencies"
if [ ! -d "$INSTALL_DIR/venv" ]; then
  python3 -m venv "$INSTALL_DIR/venv"
fi
# Pinned, like the image: floating pip/transitive deps meant a host install
# resolved a different dependency tree from the tested one.
"$INSTALL_DIR/venv/bin/pip" install --quiet "pip==26.2.1"
"$INSTALL_DIR/venv/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt" \
    -c "$INSTALL_DIR/constraints.txt"

say "Initialise the database"
# Must succeed — a failed init (disk full, bad perms) previously got swallowed
# and the service was enabled against a broken DB anyway.
"$INSTALL_DIR/venv/bin/python" "$INSTALL_DIR/app.py" --migrate

# No .env is created or required — credentials are configured in the browser
# via the /setup wizard. (Env vars remain an optional path for unattended/IaC
# deploys; the systemd unit sources .env only if you choose to create one.)

# Code stays root-owned (the service must not be able to rewrite itself);
# only the data dir is writable by the service account.
chown -R "$SERVICE_USER":"$SERVICE_USER" "$INSTALL_DIR/data"

say "Install systemd unit + cron job + logrotate"
sed "s|/opt/powersoc|$INSTALL_DIR|g; s|User=powersoc|User=$SERVICE_USER|; \
     s|0.0.0.0:8073|0.0.0.0:$PORT|" \
    "$INSTALL_DIR/powersoc.service" > /etc/systemd/system/powersoc.service
# Rewrite the install path, then the cron user field (the token before the
# command path — " powersoc flock ..."), leaving log paths untouched.
sed "s|/opt/powersoc|$INSTALL_DIR|g; s| powersoc flock | $SERVICE_USER flock |g" \
    "$INSTALL_DIR/powersoc.cron" > /etc/cron.d/powersoc
chmod 0644 /etc/cron.d/powersoc
# copytruncate: the unit's append: file descriptor keeps writing to the same
# inode, so plain rotation would orphan it.
cat > /etc/logrotate.d/powersoc <<'EOF'
/var/log/powersoc.log /var/log/powersoc-sync.log {
    weekly
    rotate 8
    compress
    missingok
    notifempty
    copytruncate
}
EOF
systemctl daemon-reload
systemctl enable --now powersoc.service
systemctl restart powersoc.service

say "Tail logs for 10 seconds"
timeout 10 tail -f /var/log/powersoc.log 2>/dev/null || true

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
say "Done."
echo
echo "  PowerSOC is running on port $PORT."
echo "  Open:        http://${IP:-<host>}:$PORT/"
echo "  First run:   http://${IP:-<host>}:$PORT/setup"
echo
echo "  Logs:        journalctl -u powersoc -f   (or /var/log/powersoc.log)"
echo "  Daily sync:  /etc/cron.d/powersoc  (06:25 Europe/London)"
echo "  Backups:     /var/backups/powersoc  (copy off-box for real protection)"
echo "  Setup token: grep 'setup token' /var/log/powersoc.log"
