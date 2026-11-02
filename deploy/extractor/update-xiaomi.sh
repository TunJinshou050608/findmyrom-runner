#!/bin/bash
# Updates only the two FindMyROM extraction modules. No credentials, proxy,
# tunnel, firewall, routing, system packages or service sandbox changes.
set -euo pipefail
umask 077
unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy
if [ "$(id -u)" != 0 ]; then echo 'Run as root.' >&2; exit 1; fi
app_dir=/opt/fmr-extractor/app
python_bin=/opt/fmr-extractor/venv/bin/python
if [ ! -x "$python_bin" ] || [ ! -f "$app_dir/fmr_extractor/service.py" ]; then
  echo 'Existing FindMyROM extraction installation not found.' >&2; exit 1
fi
work_dir=$(mktemp -d)
trap 'rm -rf -- "$work_dir"' EXIT
cp -a "$app_dir" "$work_dir/app"
base_url=https://raw.githubusercontent.com/TunJinshou050608/findmyrom-runner/main/fmr_extractor
for module in service.py streaming_probe.py; do
  curl --noproxy '*' --fail --silent --show-error --location --proto '=https' --tlsv1.2 \
    "$base_url/$module" --output "$work_dir/app/fmr_extractor/$module"
done
printf '%s  %s\n' '93d5e1f0fde79c6ad7f07cafb7ab5c2dc5446d94eb7750f439053768c820a279' "$work_dir/app/fmr_extractor/service.py" '28f2ab9aac6f502c783f0c9e3a18395afe453cf519fb88561b4bf716f3a616e0' "$work_dir/app/fmr_extractor/streaming_probe.py" | sha256sum --check --status
"$python_bin" -B - "$work_dir/app" <<'PYTEST'
import sys, json, time, hmac, hashlib
sys.path.insert(0, sys.argv[1])
from fmr_extractor.service import verify_job
job=dict(schema=1,id='00000000-0000-0000-0000-000000000000',expires=int(time.time())+60,token='fw_fixture00001',brand='xiaomi',device='alioth',version='OS1.0.5.0.TKHMIXM',partition='boot',package_type='recovery_zip',url='https://bigota.d.miui.com/fixture.zip')
body=json.dumps(job).encode();secret='isolated-update-smoke-test-only-32bytes'
sig=hmac.new(secret.encode(),b'token=extract-job:'+body+f'&exp={job["expires"]}'.encode(),hashlib.sha256).hexdigest()
assert verify_job(body,sig,secret)['brand']=='xiaomi'
print('Staged Xiaomi request validation passed; no firmware downloaded.')
PYTEST
backup_dir=/opt/fmr-extractor/backups/xiaomi-$(date -u +%Y%m%dT%H%M%SZ)-$$
install -d -m 0700 "$backup_dir"
for module in service.py streaming_probe.py; do
  cp -p "$app_dir/fmr_extractor/$module" "$backup_dir/$module"
  install -m 0644 "$work_dir/app/fmr_extractor/$module" "$app_dir/fmr_extractor/$module"
done
rollback() {
  for module in service.py streaming_probe.py; do cp -p "$backup_dir/$module" "$app_dir/fmr_extractor/$module"; done
  systemctl restart fmr-extractor.service || true
}
if ! systemctl restart fmr-extractor.service; then rollback; exit 1; fi
sleep 1
if ! systemctl is-active --quiet fmr-extractor.service; then rollback; exit 1; fi
printf '%s\n' 'Xiaomi support installed. Only fmr-extractor.service was restarted.'
curl --noproxy '*' --fail --silent --show-error --max-time 15 https://extract-origin.findmyrom.com/v1/capabilities || true
printf '\n'
