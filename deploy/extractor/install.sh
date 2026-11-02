#!/bin/bash
# Run from the unpacked deployment bundle. Changes only FindMyROM-owned paths,
# users, and units. Does not install a global tunnel, open ports or alter proxies.
set -euo pipefail
umask 077
unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy
cd -- "$(dirname -- "$0")"
if [ "$(id -u)" != 0 ]; then
  echo 'Run this installer as root.' >&2
  exit 1
fi
for tool in python3 curl sha256sum systemctl systemd systemd-analyze useradd install getent awk grep; do
  command -v "$tool" >/dev/null || { echo "Missing prerequisite: $tool" >&2; exit 1; }
done
if [ "$(uname -m)" != x86_64 ]; then echo 'This bundle targets Linux x86_64.' >&2; exit 1; fi
if [ "$(systemd --version | head -1 | awk '{print $2}')" -lt 247 ]; then
  echo 'systemd 247 or newer is required for filesystem/process isolation.' >&2; exit 1
fi
python3 -c 'import sys; assert sys.version_info >= (3,10), "Python 3.10 or newer required"'
if [ ! -f /sys/fs/cgroup/cgroup.controllers ] || ! grep -qw memory /sys/fs/cgroup/cgroup.controllers || ! grep -qw cpu /sys/fs/cgroup/cgroup.controllers; then
  echo 'cgroup v2 with CPU and memory controllers is required; refusing to run without resource isolation.' >&2; exit 1
fi
if [ ! -f agent.env ] || [ ! -f tunnel.env ]; then echo 'Missing deployment credentials.' >&2; exit 1; fi
# Never reuse an existing arbitrary account with these names.
for account in fmr-extractor fmr-tunnel; do
  if id "$account" >/dev/null 2>&1; then
    if [ "$(getent passwd "$account" | cut -d: -f6)" != /nonexistent ]; then
      echo "Conflicting account $account; refusing to reuse it." >&2; exit 1
    fi
  else
    useradd --system --user-group --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin "$account"
  fi
done
install -d -m 0755 /opt/fmr-extractor /opt/fmr-extractor/app /opt/fmr-extractor/bin /opt/fmr-extractor/runtime
install -d -m 0700 /etc/fmr-extractor
for config in resolv.conf hosts nsswitch.conf; do
  install -m 0644 "/etc/$config" "/opt/fmr-extractor/runtime/$config"
done
install -m 0600 agent.env /etc/fmr-extractor/agent.env
install -m 0600 tunnel.env /etc/fmr-extractor/tunnel.env
# A private venv; no changes to the system Python or existing applications.
if [ ! -x /opt/fmr-extractor/venv/bin/python ]; then
  python3 -m venv /opt/fmr-extractor/venv || {
    echo 'Install python3-venv using your OS package manager, then retry.' >&2; exit 1;
  }
fi
/opt/fmr-extractor/venv/bin/python -m pip install --disable-pip-version-check -r requirements.txt
cp -R app/. /opt/fmr-extractor/app/
chown -R root:root /opt/fmr-extractor
chmod -R go-w /opt/fmr-extractor
# Dedicated, version-pinned cloudflared; verify its official release checksum.
# No replacement of any global cloudflared executable or existing service.
if [ ! -x /opt/fmr-extractor/bin/cloudflared ]; then
  tmp_dir=$(mktemp -d)
  trap 'rm -rf -- "$tmp_dir"' EXIT
  curl --noproxy '*' --fail --location --proto '=https' --tlsv1.2 \
    'https://github.com/cloudflare/cloudflared/releases/download/2026.10.7/cloudflared-linux-amd64' \
    --output "$tmp_dir/cloudflared"
  echo "1f99fb518b83dec2ba8d870a7cc991d82a77e6196295a3d2754ba17ab9f91872  $tmp_dir/cloudflared" | sha256sum --check --status
  install -m 0755 "$tmp_dir/cloudflared" /opt/fmr-extractor/bin/cloudflared
fi
install -m 0644 fmr-extraction.slice fmr-extractor.service fmr-extractor-tunnel.service /etc/systemd/system/
systemd-analyze verify /etc/systemd/system/fmr-extractor.service /etc/systemd/system/fmr-extractor-tunnel.service
systemctl daemon-reload
systemctl enable --now fmr-extractor.service fmr-extractor-tunnel.service
systemctl is-active fmr-extractor.service fmr-extractor-tunnel.service
printf '%s\n' 'Installed. Only the two fmr-extractor services were started.'
