#!/usr/bin/env bash
set -euo pipefail

FMR_RUNNER_DIR="${FMR_RUNNER_DIR:-/opt/findmyrom-stock-runner}"
FMR_STAGING_V4_DIR="${FMR_STAGING_V4_DIR:-/var/lib/findmyrom/staging-v4}"
FMR_VENVS_DIR="${FMR_VENVS_DIR:-/opt/fmr-venvs}"
FMR_REPO_URL="${FMR_REPO_URL:-https://github.com/TunJinshou050608/findmyrom-runner.git}"

mkdir -p "$FMR_STAGING_V4_DIR/locks"
LOCK_FILE="$FMR_STAGING_V4_DIR/locks/local-collect-daemon.lock"

exec 8>"$LOCK_FILE"
if ! flock -n 8; then
  printf '{"status":"skipped_locked","lock_file":"%s"}\n' "$LOCK_FILE"
  exit 0
fi

if [[ ! -d "$FMR_RUNNER_DIR/.git" ]]; then
  mkdir -p "$(dirname "$FMR_RUNNER_DIR")"
  git clone --depth=1 --branch main "$FMR_REPO_URL" "$FMR_RUNNER_DIR"
else
  if git -C "$FMR_RUNNER_DIR" fetch --depth=1 origin main; then
    git -C "$FMR_RUNNER_DIR" reset --hard origin/main
  else
    printf 'WARNING: git fetch failed transiently; proceeding with current checkout in %s\n' "$FMR_RUNNER_DIR" >&2
  fi
fi

mkdir -p "$FMR_VENVS_DIR"
REQ_HASH="$(sha256sum "$FMR_RUNNER_DIR/requirements.txt" | cut -c1-16)"
VENV_DIR="$FMR_VENVS_DIR/$REQ_HASH"
if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  (
    flock -x 9
    if [[ ! -x "$VENV_DIR/bin/python" ]]; then
      TMP_VENV="$(mktemp -d "$FMR_VENVS_DIR/.tmp-${REQ_HASH}-XXXXXX")"
      trap 'rm -rf "$TMP_VENV"' EXIT
      python3 -m venv "$TMP_VENV"
      "$TMP_VENV/bin/python" -m pip install --disable-pip-version-check -r "$FMR_RUNNER_DIR/requirements.txt"
      mv "$TMP_VENV" "$VENV_DIR"
      trap - EXIT
    fi
  ) 9>"$FMR_VENVS_DIR/.lock"
fi

PYTHON_BIN="$VENV_DIR/bin/python"
export FMR_RUNNER_DIR FMR_STAGING_V4_DIR FMR_VENVS_DIR FMR_REPO_URL
export FMR_DAEMON_LOCK_HELD=1

cd "$FMR_RUNNER_DIR"
exec "$PYTHON_BIN" "$FMR_RUNNER_DIR/ops/local_collect_daemon.py" \
  --runner-dir "$FMR_RUNNER_DIR" \
  --state-dir "$FMR_STAGING_V4_DIR" \
  "$@"
