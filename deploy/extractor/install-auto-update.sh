#!/bin/bash
# One-time installation of a separate updater. Does not alter extraction/tunnel
# credentials, proxy settings, existing service isolation or the system Python.
set -euo pipefail
umask 077
unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy PYTHONPATH PYTHONHOME BASH_ENV ENV
if [ "$(id -u)" != 0 ]; then echo 'Run as root.' >&2; exit 1; fi
python_bin=/opt/fmr-extractor/venv/bin/python
if [ ! -x "$python_bin" ] || [ ! -f /opt/fmr-extractor/app/fmr_extractor/service.py ]; then
  echo 'Existing FindMyROM extraction installation not found.' >&2; exit 1
fi
for tool in curl systemctl systemd-analyze install flock; do
  command -v "$tool" >/dev/null || { echo "Missing prerequisite: $tool" >&2; exit 1; }
done
work_dir=$(mktemp -d)
trap 'rm -rf -- "$work_dir"' EXIT
update_ref=${FMR_BOOTSTRAP_REF:-}
if [ -z "$update_ref" ]; then
  curl --disable --noproxy '*' --fail --silent --show-error --proto '=https' --tlsv1.2 --max-time 30 \
    https://api.github.com/repos/TunJinshou050608/findmyrom-runner/git/ref/heads/main --output "$work_dir/ref.json"
  update_ref=$("$python_bin" -c 'import json,sys; print(json.load(open(sys.argv[1]))["object"]["sha"])' "$work_dir/ref.json")
fi
if [[ ! "$update_ref" =~ ^[0-9a-f]{40}$ ]]; then echo 'Invalid bootstrap revision.' >&2; exit 1; fi
base_url=https://raw.githubusercontent.com/TunJinshou050608/findmyrom-runner/$update_ref/deploy/extractor
for name in auto_update.py fmr-extractor-update.service fmr-extractor-update.timer; do
  curl --disable --noproxy '*' --fail --silent --show-error --proto '=https' --tlsv1.2 --max-time 30 \
    "$base_url/$name" --output "$work_dir/$name"
done
"$python_bin" -B - "$work_dir/auto_update.py" <<'PYTEST'
import ast,sys
ast.parse(open(sys.argv[1]).read())
print('Updater syntax checked. No firmware downloaded.')
PYTEST
systemd-analyze verify "$work_dir/fmr-extractor-update.service" "$work_dir/fmr-extractor-update.timer"
# Stop only the timer while replacing its checker; do not overlap an active check.
systemctl stop fmr-extractor-update.timer 2>/dev/null || true
if systemctl is-active --quiet fmr-extractor-update.service; then
  echo 'An update check is active. Retry installation after it completes.' >&2
  systemctl start fmr-extractor-update.timer || true
  exit 1
fi
install -d -m 0755 /opt/fmr-extractor/updater
install -d -m 0700 /var/lib/fmr-extractor-update
install -o root -g root -m 0644 "$work_dir/auto_update.py" /opt/fmr-extractor/updater/auto_update.py
install -o root -g root -m 0644 "$work_dir/fmr-extractor-update.service" "$work_dir/fmr-extractor-update.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now fmr-extractor-update.timer
if ! systemctl start fmr-extractor-update.service; then
  echo 'Timer installed; the first check failed. It will retry automatically; inspect the update service journal.' >&2
fi
printf '%s\n' 'Automatic updates enabled: checks every 10 minutes (+ up to 60 seconds jitter).' 
systemctl list-timers --no-pager fmr-extractor-update.timer
