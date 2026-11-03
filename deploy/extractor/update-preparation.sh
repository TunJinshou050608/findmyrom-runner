#!/bin/bash
# Updates only the three FindMyROM extraction modules. No credentials, proxy,
# tunnel, firewall, routing, system packages or service sandbox changes.
set -euo pipefail
umask 077
unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy
if [ "$(id -u)" != 0 ]; then echo 'Run as root.' >&2; exit 1; fi
exec 9>/opt/fmr-extractor/update.lock
flock -n 9 || { echo 'Another extractor update is running.' >&2; exit 75; }
app_dir=/opt/fmr-extractor/app
python_bin=/opt/fmr-extractor/venv/bin/python
if [ ! -x "$python_bin" ] || [ ! -f "$app_dir/fmr_extractor/service.py" ]; then
  echo 'Existing FindMyROM extraction installation not found.' >&2; exit 1
fi
work_dir=$(mktemp -d)
trap 'rm -rf -- "$work_dir"' EXIT
cp -a "$app_dir" "$work_dir/app"
update_ref=${FMR_UPDATE_REF:-main}
if [[ "$update_ref" != main && ! "$update_ref" =~ ^[0-9a-f]{40}$ ]]; then
  echo 'Invalid update revision.' >&2; exit 1
fi
base_url=https://raw.githubusercontent.com/TunJinshou050608/findmyrom-runner/$update_ref/fmr_extractor
for module in service.py streaming_probe.py prepared.py; do
  curl --disable --noproxy '*' --fail --silent --show-error --location --proto '=https' --tlsv1.2 --connect-timeout 10 --max-time 30 \
    "$base_url/$module" --output "$work_dir/app/fmr_extractor/$module"
done
printf '%s  %s\n' 'de26f1d4ab540ea9075839c989317e5e4fc4993688d9f114acd57ed50436ba70' "$work_dir/app/fmr_extractor/service.py" 'a521faff88bc1df428097b1dfb2a47254f559a5f4dd7dcc87a8bf80eddcc2eb9' "$work_dir/app/fmr_extractor/streaming_probe.py" '56ab1dc5e67c84c03f6f284881bae444827977ab158435d805c9d8abc02b21b1' "$work_dir/app/fmr_extractor/prepared.py" | sha256sum --check --status
"$python_bin" -B - "$work_dir/app" <<'PYTEST'
import sys, json, time, hmac, hashlib
sys.path.insert(0, sys.argv[1])
from fmr_extractor.service import verify_job
job=dict(schema=1,id='00000000-0000-0000-0000-000000000000',expires=int(time.time())+60,token='fw_fixture00001',brand='xiaomi',device='alioth',version='OS1.0.5.0.TKHMIXM',partition='boot',package_type='recovery_zip',url='https://bigota.d.miui.com/fixture.zip')
body=json.dumps(job).encode();secret='isolated-update-smoke-test-only-32bytes'
sig=hmac.new(secret.encode(),b'token=extract-job:'+body+f'&exp={job["expires"]}'.encode(),hashlib.sha256).hexdigest()
assert verify_job(body,sig,secret)['brand']=='xiaomi'
from fmr_extractor.prepared import PreparedImages, verify_control
assert callable(PreparedImages.start) and callable(verify_control)
print('Staged request validation passed; no firmware downloaded.')
PYTEST
backup_dir=/opt/fmr-extractor/backups/preparation-$(date -u +%Y%m%dT%H%M%SZ)-$$
install -d -m 0700 "$backup_dir"
for module in service.py streaming_probe.py prepared.py; do
  if [ -f "$app_dir/fmr_extractor/$module" ]; then
    cp -p "$app_dir/fmr_extractor/$module" "$backup_dir/$module"
  else
    touch "$backup_dir/$module.absent"
  fi
done
rollback() {
  trap - ERR
  for module in service.py streaming_probe.py prepared.py; do
    if [ -f "$backup_dir/$module.absent" ]; then
      rm -f -- "$app_dir/fmr_extractor/$module"
    else
      cp -p "$backup_dir/$module" "$app_dir/fmr_extractor/$module"
    fi
  done
  systemctl restart fmr-extractor.service || true
}
trap rollback ERR
trap 'rollback; exit 130' INT TERM
for module in service.py streaming_probe.py prepared.py; do
  install -m 0644 "$work_dir/app/fmr_extractor/$module" "$app_dir/fmr_extractor/$module"
done
systemctl restart fmr-extractor.service
sleep 1
systemctl is-active --quiet fmr-extractor.service
healthy=0
for attempt in 1 2 3 4 5; do
  if curl --disable --noproxy '*' --fail --silent --show-error --max-time 3 \
    http://127.0.0.1:18765/v1/capabilities --output "$work_dir/capabilities.json" && \
    "$python_bin" -c 'import json,sys; caps=json.load(open(sys.argv[1])); assert caps.get("prepared_downloads") is True and caps.get("samsung_full_stream") is True' "$work_dir/capabilities.json"; then
    healthy=1
    break
  fi
  sleep 1
done
[ "$healthy" = 1 ]
trap - ERR INT TERM
printf '%s\n' 'Preparation progress and checksum support installed. Only fmr-extractor.service was restarted.'
cat "$work_dir/capabilities.json"
printf '\n'
