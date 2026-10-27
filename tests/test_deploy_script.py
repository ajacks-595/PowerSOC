"""scripts/deploy-docker.sh, exercised end to end with stub `ssh` and
`docker` binaries (2026-10-26 review, P1.4 + deploy P2s). The remote half runs
locally under a fake $HOME; docker calls are logged and their outcomes
scripted through env vars. Covers the key-seed guard, HOST_DIR inside
APP_DIR, restore validation before production is stopped, and the health
gate's rollback."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

SSH = r"""#!/usr/bin/env bash
# Drop ssh options, then the host; run the rest like a remote login shell would.
while [ $# -gt 0 ]; do
  case "$1" in -o|-i|-p) shift 2 ;; -*) shift ;; *) break ;; esac
done
shift   # host
exec bash -c "$*"
"""

DOCKER = r"""#!/usr/bin/env bash
echo "docker $*" >> "$DOCKER_LOG"
case "$1 $2" in
  "image inspect") [ -n "${FAKE_PREV_IMAGE:-}" ] && echo "$FAKE_PREV_IMAGE" && exit 0; exit 1 ;;
  "volume inspect") [ "${FAKE_VOLUME:-0}" = 1 ] && exit 0; exit 1 ;;
esac
case "$1" in
  run)
    case "$*" in
      *"test -s /data/powersoc.db"*) [ "${FAKE_VOLUME_HAS_DB:-0}" = 1 ] && exit 0; exit 1 ;;
      *"PRAGMA quick_check"*) [ "${FAKE_RESTORE_BAD:-0}" = 1 ] && exit 1; exit 0 ;;
    esac
    exit 0 ;;
  exec)
    case "$*" in *"/api/health"*)
      echo "{\"version\": \"${FAKE_VERSION}\", \"credentials_ok\": ${FAKE_CREDS_OK:-true}}"
      [ "${FAKE_VERSION}" = "${EXPECT_VERSION}" ] && [ "${FAKE_CREDS_OK:-true}" = true ] && exit 0
      exit 3 ;;
    esac
    exit 0 ;;
esac
exit 0
"""


@pytest.fixture
def harness(tmp_path):
    if not shutil.which("git"):
        pytest.skip("git not available")
    repo = tmp_path / "repo"
    subprocess.run(["git", "clone", "-q", str(REPO), str(repo)], check=True)
    # The script and compose file under test are the WORKING copies.
    for rel in ("scripts/deploy-docker.sh", "docker-compose.yml", "config.py"):
        shutil.copy(REPO / rel, repo / rel)
    env_git = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
    subprocess.run(["git", "-C", str(repo), "commit", "-qam", "under test", "--allow-empty"],
                   check=True, env={**os.environ, **env_git})
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("ssh", SSH), ("docker", DOCKER)):
        p = bin_dir / name
        p.write_text(body)
        p.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    version = next(line.split('"')[1] for line in (repo / "config.py").read_text().splitlines()
                   if line.startswith("APP_VERSION"))
    log = tmp_path / "docker.log"

    def run(**env):
        e = {"PATH": f"{bin_dir}:{os.environ['PATH']}", "HOME": str(home),
             "DEPLOY_HOST": "pi@192.168.1.51", "DOCKER_LOG": str(log),
             "EXPECT_VERSION": version, "FAKE_VERSION": version,
             "FAKE_PREV_IMAGE": "sha256:prev"}
        e.update({k: str(v) for k, v in env.items()})
        r = subprocess.run(["bash", str(repo / "scripts/deploy-docker.sh")],
                           env=e, capture_output=True, text=True, timeout=120)
        calls = log.read_text() if log.exists() else ""
        return r, calls
    return run, home


def test_fresh_host_deploys_and_generates_a_key_seed(harness):
    run, home = harness
    r, calls = run()
    assert r.returncode == 0, r.stderr
    assert (home / "powersoc-host" / "machine_id").stat().st_size > 0
    assert "docker compose up -d" in calls
    assert (home / "powersoc" / "app.py").exists()
    assert not (home / "powersoc.old").exists()


def test_missing_seed_with_existing_data_is_refused(harness):
    run, home = harness
    r, calls = run(FAKE_VOLUME=1, FAKE_VOLUME_HAS_DB=1)
    assert r.returncode != 0 and "NEW_KEY=1" in r.stderr
    assert not (home / "powersoc-host" / "machine_id").exists()
    assert "compose up" not in calls and "compose stop" not in calls
    r, calls = run(FAKE_VOLUME=1, FAKE_VOLUME_HAS_DB=1, NEW_KEY=1)
    assert r.returncode == 0, r.stderr


def test_host_dir_inside_app_dir_is_refused(harness):
    run, home = harness
    r, calls = run(HOST_DIR="~/powersoc/host")
    assert r.returncode != 0 and "must not be inside" in r.stderr
    assert "compose" not in calls


def test_a_bad_restore_source_never_stops_production(harness, tmp_path):
    run, home = harness
    (home / "powersoc-host").mkdir()
    (home / "powersoc-host" / "machine_id").write_text("seed")
    r, calls = run(RESTORE_FROM=str(tmp_path / "missing.db"))
    assert r.returncode != 0 and "not a readable file" in r.stderr
    assert "compose stop" not in calls
    bad = tmp_path / "bad.db"
    bad.write_text("not sqlite")
    r, calls = run(RESTORE_FROM=str(bad), FAKE_RESTORE_BAD=1)
    assert r.returncode != 0 and "not a valid SQLite database" in r.stderr
    assert "compose stop" not in calls


def _deploy_once(run, home):
    r, _ = run()
    assert r.returncode == 0, r.stderr
    (home / "powersoc" / "MARKER-v1").write_text("previous source")


def test_unreadable_credentials_roll_back_to_the_previous_version(harness):
    run, home = harness
    _deploy_once(run, home)
    r, calls = run(FAKE_CREDS_OK="false")
    assert r.returncode != 0 and "rolling back" in r.stderr
    assert "docker tag powersoc:previous powersoc:local" in calls
    assert (home / "powersoc" / "MARKER-v1").exists()          # old source back
    assert (home / "powersoc.failed").exists()


def test_wrong_version_serving_rolls_back(harness):
    run, home = harness
    _deploy_once(run, home)
    r, calls = run(FAKE_VERSION="0.0.1")
    assert r.returncode != 0
    assert "docker tag powersoc:previous powersoc:local" in calls
