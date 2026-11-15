#!/usr/bin/env bash
set -euo pipefail

FMR_RUNNER_DIR="${FMR_RUNNER_DIR:-/opt/findmyrom-stock-runner}"
FMR_STAGING_V4_DIR="${FMR_STAGING_V4_DIR:-/var/lib/findmyrom/staging-v4}"
FMR_VENVS_DIR="${FMR_VENVS_DIR:-/opt/fmr-venvs}"
FMR_REPO_URL="${FMR_REPO_URL:-https://github.com/TunJinshou050608/findmyrom-runner.git}"

mkdir -p "$FMR_STAGING_V4_DIR/locks"
LOCK_FILE="$FMR_STAGING_V4_DIR/locks/local-collect-timer.lock"

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

ENABLED_VENDORS=()
while IFS= read -r vendor; do
  if [[ -n "$vendor" ]]; then
    ENABLED_VENDORS+=("$vendor")
  fi
done < <("$PYTHON_BIN" - "$FMR_RUNNER_DIR/config/vendors.yaml" <<'PY'
import sys
import yaml

with open(sys.argv[1], "r", encoding="utf-8") as fh:
    data = yaml.safe_load(fh) or {}

for vid, cfg in (data.get("vendors") or {}).items():
    if isinstance(cfg, dict) and cfg.get("enabled") is True:
        print(vid)
PY
)

if [[ ${#ENABLED_VENDORS[@]} -eq 0 ]]; then
  echo "ERROR: No enabled vendors found in $FMR_RUNNER_DIR/config/vendors.yaml" >&2
  exit 1
fi

RUN_TMP="$(mktemp -d)"
trap 'rm -rf "$RUN_TMP"' EXIT

cd "$FMR_RUNNER_DIR"

PIDS=()
for v in "${ENABLED_VENDORS[@]}"; do
  "$PYTHON_BIN" -m fmr_ingest_stock.pipeline.cli collect \
    --vendor "$v" \
    --state-dir "$FMR_STAGING_V4_DIR" \
    >"$RUN_TMP/${v}.stdout" 2>"$RUN_TMP/${v}.stderr" &
  PIDS+=("$!")
done

FAILED=0
for idx in "${!ENABLED_VENDORS[@]}"; do
  v="${ENABLED_VENDORS[$idx]}"
  pid="${PIDS[$idx]}"
  rc=0
  wait "$pid" || rc=$?
  if [[ -s "$RUN_TMP/${v}.stderr" ]]; then
    sed "s/^/[$v stderr] /" "$RUN_TMP/${v}.stderr" >&2
  fi
  if [[ $rc -ne 0 ]]; then
    printf '[%s] FAILED (exit=%s)\n' "$v" "$rc" >&2
    if [[ -s "$RUN_TMP/${v}.stdout" ]]; then
      cat "$RUN_TMP/${v}.stdout" >&2
    fi
    FAILED=1
  else
    "$PYTHON_BIN" - "$v" "$RUN_TMP/${v}.stdout" <<'PY' || true
import json
import pathlib
import sys

vendor = sys.argv[1]
raw = pathlib.Path(sys.argv[2]).read_text(encoding="utf-8").strip()
try:
    data = json.loads(raw)
    results = data.get("results") or [data]
    for item in results:
        print(json.dumps(item, separators=(",", ":"), sort_keys=True))
except Exception:
    if raw:
        print(f"[{vendor}] {raw}")
PY
  fi
done

exit "$FAILED"
