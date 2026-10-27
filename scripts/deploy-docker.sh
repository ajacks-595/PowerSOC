#!/usr/bin/env bash
#
# Redeploy PowerSOC (git HEAD) to a Docker host over SSH.
#
#   DEPLOY_HOST=user@192.168.1.51 SSH_KEY=~/.ssh/id_ed25519 scripts/deploy-docker.sh
#
# Optional:
#   APP_DIR       source dir on the host        (default: ~/powersoc)
#   HOST_DIR      persistent host config dir     (default: ~/powersoc-host)
#                 holds compose.env, machine_id, backups/ — survives redeploys
#                 and lives OUTSIDE the Docker data volume (and outside APP_DIR).
#   RESTORE_FROM  path ON THE HOST of a DB backup to restore before start
#                 (the existing DB, if any, is moved aside — never deleted).
#   NEW_KEY=1     allow generating a NEW key seed although data already exists
#                 (every stored credential then has to be re-entered).
#
# Order of operations (2026-10-26): nothing running is touched until the new
# image is built and every input is validated; the new version must answer
# /api/health with the expected version and credentials_ok, or the previous
# image and source are put back. Idempotent.
set -euo pipefail
cd "$(dirname "$0")/.."

: "${DEPLOY_HOST:?set DEPLOY_HOST=user@host}"
SSH_OPTS=(-o BatchMode=yes -o StrictHostKeyChecking=accept-new)
[ -n "${SSH_KEY:-}" ] && SSH_OPTS+=(-i "$SSH_KEY")
APP_DIR="${APP_DIR:-~/powersoc}"
HOST_DIR="${HOST_DIR:-~/powersoc-host}"
RESTORE_FROM="${RESTORE_FROM:-}"
NEW_KEY="${NEW_KEY:-0}"

say() { printf '\n\033[1;36m>>> %s\033[0m\n' "$*"; }

[ -z "$(git status --porcelain --untracked-files=no)" ] || {
  echo "Working tree has uncommitted changes — commit first (deploys HEAD)." >&2; exit 1; }
REV="$(git rev-parse --short HEAD)"
VERSION="$(sed -n 's/^APP_VERSION = "\(.*\)"$/\1/p' config.py)"
[ -n "$VERSION" ] || { echo "Can't read APP_VERSION from config.py" >&2; exit 1; }

say "Shipping $REV (v$VERSION) to $DEPLOY_HOST:$APP_DIR"
git archive --format=tar HEAD | ssh "${SSH_OPTS[@]}" "$DEPLOY_HOST" \
  "set -e; d=$APP_DIR.new; rm -rf \"\$d\"; mkdir -p \"\$d\"; tar -x -C \"\$d\""

# Arguments travel as one string through the remote shell (ssh flattens them):
# an empty one vanishes, so every optional value is read as ${N:-}.
ssh "${SSH_OPTS[@]}" "$DEPLOY_HOST" bash -s -- \
  "$APP_DIR" "$HOST_DIR" "$VERSION" "$NEW_KEY" "$RESTORE_FROM" <<'REMOTE'
set -euo pipefail
say() { printf '\n\033[1;36m>>> %s\033[0m\n' "$*"; }
die() { printf '\n\033[1;31m!!! %s\033[0m\n' "$*" >&2; exit 1; }
# Tilde expansion by hand — the old `eval echo "$1"` would also have run any
# command substitution smuggled into a path.
expand() { case "$1" in "~") printf '%s' "$HOME" ;; "~/"*) printf '%s/%s' "$HOME" "${1#\~/}" ;; *) printf '%s' "$1" ;; esac; }
APP_DIR="$(expand "$1")"; HOST_DIR="$(expand "$2")"; VERSION="$3"
NEW_KEY="${4:-0}"; RESTORE_FROM="${5:-}"
APP_DIR="${APP_DIR%/}"; HOST_DIR="${HOST_DIR%/}"

# `rm -rf $APP_DIR.old` below would take a HOST_DIR that lives inside it —
# the key seed and every backup with it.
case "$HOST_DIR/" in "$APP_DIR"/*|"$APP_DIR.old"/*|"$APP_DIR.new"/*)
  die "HOST_DIR ($HOST_DIR) must not be inside APP_DIR ($APP_DIR)";; esac
if [ -n "$RESTORE_FROM" ]; then
  RESTORE_FROM="$(expand "$RESTORE_FROM")"
  [ -r "$RESTORE_FROM" ] || die "RESTORE_FROM=$RESTORE_FROM is not a readable file — nothing was changed"
fi

mkdir -p "$HOST_DIR/backups"
if [ ! -f "$HOST_DIR/compose.env" ]; then
  printf 'POWERSOC_HOST_DIR=%s\nPOWERSOC_MACHINE_ID_FILE=/host/machine_id\nPOWERSOC_HEARTBEAT_URL=\n' \
    "$HOST_DIR" > "$HOST_DIR/compose.env"
  chmod 600 "$HOST_DIR/compose.env"
fi
cp "$HOST_DIR/compose.env" "$APP_DIR.new/.env"

say "Building image (the running container is untouched)"
PREV_IMAGE="$(docker image inspect -f '{{.Id}}' powersoc:local 2>/dev/null || true)"
[ -n "$PREV_IMAGE" ] && docker tag powersoc:local powersoc:previous
( cd "$APP_DIR.new" && docker compose build --pull </dev/null )

# The key seed. Generating a NEW one when data already exists silently made
# every stored credential unreadable (a deploy as another user or with another
# HOST_DIR still lands on the same compose project and volume).
if [ ! -s "$HOST_DIR/machine_id" ]; then
  HAS_DATA=0
  if docker volume inspect powersoc_powersoc-data >/dev/null 2>&1 \
     && docker run --rm --entrypoint sh -v powersoc_powersoc-data:/data powersoc:local \
          -c 'test -s /data/powersoc.db' </dev/null; then
    HAS_DATA=1
  fi
  [ -n "$RESTORE_FROM" ] && HAS_DATA=1
  if [ "$HAS_DATA" = 1 ] && [ "$NEW_KEY" != 1 ]; then
    die "$HOST_DIR/machine_id is missing but PowerSOC data exists (or is being restored).
    Copy the ORIGINAL machine_id there (it decrypts the stored credentials), or
    re-run with NEW_KEY=1 to start over with a new key (all credentials re-entered).
    Nothing was changed."
  fi
  say "Generating the Fernet key seed $HOST_DIR/machine_id (back it up off this host!)"
  (umask 077; head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n' > "$HOST_DIR/machine_id")
fi

# Ownership (done via the image itself, so no sudo is needed on the host):
#   HOST_DIR          host user, 0751 — container (uid 10001) may traverse only
#   backups/          10001:<host group>, setgid 2770 — the app writes backups
#                     (0640); the host user can read/copy them off-box
#   machine_id        <host user>:10001, 0440 — both can read, nobody writes
HUID="$(id -u)"; HGID="$(id -g)"
docker run --rm --user 0 --entrypoint sh -v "$HOST_DIR":/host powersoc:local </dev/null -c \
  "chown $HUID:$HGID /host && chmod 0751 /host \
   && chown 10001:$HGID /host/backups && chmod 2770 /host/backups \
   && chown $HUID:10001 /host/machine_id && chmod 0440 /host/machine_id \
   && find /host/backups -name 'powersoc-*.db' -exec chown 10001:$HGID {} + -exec chmod 0640 {} +"

if [ -n "$RESTORE_FROM" ]; then
  say "Validating $RESTORE_FROM before touching production"
  rm -f "$HOST_DIR/restore-incoming.db"
  cp "$RESTORE_FROM" "$HOST_DIR/restore-incoming.db"
  [ -f "$RESTORE_FROM-wal" ] && cp "$RESTORE_FROM-wal" "$HOST_DIR/restore-incoming.db-wal"
  docker run --rm --user 0 --entrypoint chown -v "$HOST_DIR":/host powersoc:local \
    10001:10001 /host/restore-incoming.db </dev/null
  if ! docker run --rm --entrypoint python -v "$HOST_DIR":/host:ro powersoc:local -c \
      "import sqlite3,sys; c=sqlite3.connect('file:/host/restore-incoming.db?mode=ro&immutable=1',uri=True); sys.exit(0 if c.execute('PRAGMA quick_check').fetchone()[0]=='ok' else 1)" \
      </dev/null; then
    rm -f "$HOST_DIR/restore-incoming.db" "$HOST_DIR/restore-incoming.db-wal"
    die "$RESTORE_FROM is not a valid SQLite database — nothing was changed"
  fi
fi

say "Swapping source dirs"
rm -rf "$APP_DIR.old"
[ -d "$APP_DIR" ] && mv "$APP_DIR" "$APP_DIR.old"
mv "$APP_DIR.new" "$APP_DIR"
cd "$APP_DIR"

rollback() {
  printf '\n\033[1;31m!!! %s — rolling back\033[0m\n' "$1" >&2
  docker logs --tail 40 powersoc 2>&1 | sed 's/^/    /' >&2 || true
  if [ -n "$PREV_IMAGE" ] && [ -d "$APP_DIR.old" ]; then
    docker tag powersoc:previous powersoc:local
    rm -rf "$APP_DIR.failed"; mv "$APP_DIR" "$APP_DIR.failed"; mv "$APP_DIR.old" "$APP_DIR"
    ( cd "$APP_DIR" && docker compose up -d </dev/null )
    echo "Previous version restored; the failed source is in $APP_DIR.failed" >&2
  else
    echo "No previous version to roll back to (first deploy)." >&2
  fi
  exit 1
}

if [ -n "$RESTORE_FROM" ]; then
  say "Restoring into the data volume"
  docker compose stop powersoc </dev/null 2>/dev/null || true
  # -T + </dev/null: this script itself arrives on stdin (ssh bash -s); an
  # attached `compose run` would swallow the rest of it.
  if ! docker compose run -T --rm --no-deps --entrypoint python powersoc \
      app.py --restore /host/restore-incoming.db </dev/null; then
    rm -f "$HOST_DIR/restore-incoming.db" "$HOST_DIR/restore-incoming.db-wal"
    rollback "Restore failed (the previous database is untouched)"
  fi
  rm -f "$HOST_DIR/restore-incoming.db" "$HOST_DIR/restore-incoming.db-wal"
fi

say "Starting v$VERSION"
docker compose up -d </dev/null || rollback "docker compose up failed"
# A green container is not enough: the right version must be serving, and its
# stored credentials must still decrypt (a changed key used to deploy "fine").
# Exit 0 = good, 3 = answering but wrong, anything else = not up yet.
CHECK="import json,sys,urllib.request
h=json.load(urllib.request.urlopen('http://127.0.0.1:8073/api/health',timeout=4))['data']
print(json.dumps(h))
bad=[]
if h.get('version')!='$VERSION': bad.append('serving version %r, expected %r'%(h.get('version'),'$VERSION'))
if h.get('credentials_ok') is False: bad.append('credentials_ok is false (stored credentials cannot be decrypted)')
if bad: print('; '.join(bad),file=sys.stderr); sys.exit(3)"
rc=1; OUTF="$(mktemp "$HOST_DIR/.health-out.XXXXXX")"; ERRF="$(mktemp "$HOST_DIR/.health-err.XXXXXX")"
for _ in $(seq 1 60); do
  # Output kept until the app answers: while gunicorn is still starting each
  # try is a connection-refused traceback.
  set +e; docker exec powersoc python -c "$CHECK" </dev/null >"$OUTF" 2>"$ERRF"; rc=$?; set -e
  [ "$rc" = 0 ] || [ "$rc" = 3 ] && break
  sleep 2
done
cat "$OUTF"; [ "$rc" = 0 ] || cat "$ERRF" >&2
rm -f "$OUTF" "$ERRF"
[ "$rc" = 0 ] || rollback "v$VERSION failed its health check (rc=$rc)"
docker ps --filter name=powersoc --format '{{.Names}}  {{.Status}}'
docker logs powersoc 2>&1 | grep -i "setup token" | tail -1 || true
rm -rf "$APP_DIR.old" "$APP_DIR.failed"
REMOTE
say "Deployed $REV (v$VERSION)"
