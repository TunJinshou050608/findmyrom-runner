#!/usr/bin/env bash
set -euo pipefail
repo=TunJinshou050608/findmyrom-runner
runner_user=findmyrom-actions
base_home=/opt/findmyrom-actions
state_home=/var/lib/findmyrom/catalogue-v3
if [[ $(id -u) != 0 || $(uname -s) != Linux || ! -d /run/systemd/system ]]; then
  printf '%s\n' 'Requires root on Linux with systemd; stopped.' >&2; exit 1
fi
for tool in gh python3 git curl tar sha256sum runuser useradd systemctl cp; do command -v "$tool" >/dev/null || { printf 'Missing dependency: %s\n' "$tool" >&2; exit 1; }; done
python3 -c 'import sys; assert sys.version_info >= (3,11), "Python 3.11+ required"'
case $(uname -m) in x86_64) arch=x64;; aarch64|arm64) arch=arm64;; *) printf '%s\n' 'Unsupported architecture; stopped.' >&2; exit 1;; esac
gh auth status >/dev/null
gh api "repos/$repo" --jq '.permissions.admin' | python3 -c 'import sys; assert sys.stdin.read().strip()=="true", "Repository admin access required"'
# Respect the container's CPU/memory limits rather than the physical host totals.
collectors=$(python3 - <<'PY'
import os,pathlib,sys
cores=len(os.sched_getaffinity(0));memory={}
for line in pathlib.Path('/proc/meminfo').read_text().splitlines():
 name,value=line.split(':',1);memory[name]=int(value.strip().split()[0])*1024
available=memory.get('MemAvailable',memory['MemTotal'])
try:
 quota,period=pathlib.Path('/sys/fs/cgroup/cpu.max').read_text().split()
 if quota!='max':cores=min(cores,max(1,int(quota)//int(period)))
except (OSError,ValueError):pass
try:
 limit=pathlib.Path('/sys/fs/cgroup/memory.max').read_text().strip()
 if limit!='max':available=min(available,max(0,int(limit)-int(pathlib.Path('/sys/fs/cgroup/memory.current').read_text())))
except (OSError,ValueError):pass
count=max(1,min(4,max(1,cores-1),max(1,available//(1024**3)-1)))
if available<1024**3:raise SystemExit('Less than 1 GiB available; stopped before installing extra runners.')
print(f'Detected CPU capacity: {cores}; available memory: {available//(1024**2)} MiB; collectors: {count}; dedicated publisher: 1',file=sys.stderr)
print(count)
PY
)
if ! id "$runner_user" >/dev/null 2>&1; then useradd --system --create-home --home-dir "$base_home" --shell /bin/bash "$runner_user"; fi
runner_group=$(id -gn "$runner_user")
install -d -m 700 -o "$runner_user" -g "$runner_group" "$base_home" "$state_home"
runuser -u "$runner_user" -- env -i HOME="$base_home" PATH=/usr/local/bin:/usr/bin:/bin bash -s <<'CHECK'
set -e
checkdir=$(mktemp -d "$HOME/venv-check.XXXXXX")
trap 'rm -rf "$checkdir"' EXIT
python3 -m venv "$checkdir/venv"
"$checkdir/venv/bin/python" -m pip --version >/dev/null
CHECK
# Existing runner binaries can be copied locally; do not re-download per slot.
if [[ -d "$base_home/bin" && -d "$base_home/externals" ]]; then
  binary_kib=$(du -sk "$base_home/bin" "$base_home/externals" | awk '{sum+=$1} END {print sum}')
else binary_kib=900000; fi
new_copies=0
for ((slot=1;slot<=collectors;slot++)); do
  home=$base_home; if ((slot>1)); then home="/opt/findmyrom-actions-metadata-$slot"; fi
  [[ -f "$home/.runner" ]] || new_copies=$((new_copies+1))
done
[[ -f /opt/findmyrom-actions-publisher/.runner ]] || new_copies=$((new_copies+1))
needed_kib=$((new_copies*binary_kib+1048576))
free_kib=$(df -Pk /opt | awk 'NR==2 {print $4}')
((free_kib>=needed_kib)) || { printf 'Insufficient disk: need %s KiB free, have %s KiB. Existing services were not stopped.\n' "$needed_kib" "$free_kib" >&2; exit 1; }
tempdir=$(mktemp -d); trap 'rm -rf "$tempdir"' EXIT
if [[ ! -d "$base_home/bin" ]]; then
  gh api "repos/$repo/actions/runners/downloads" > "$tempdir/downloads.json"
  python3 - "$tempdir" "$arch" <<'PY'
import json,sys,pathlib
root=pathlib.Path(sys.argv[1]);rows=json.loads((root/'downloads.json').read_text())
item=next(x for x in rows if x['os']=='linux' and x['architecture']==sys.argv[2])
assert item['download_url'].startswith('https://github.com/actions/runner/releases/download/')
assert len(item['sha256_checksum'])==64
(root/'url').write_text(item['download_url']);(root/'checksum').write_text(item['sha256_checksum'])
PY
  curl --fail --location --retry 2 "$(cat "$tempdir/url")" -o "$tempdir/runner.tar.gz"
  printf '%s  %s\n' "$(cat "$tempdir/checksum")" "$tempdir/runner.tar.gz" | sha256sum --check
  tar -xzf "$tempdir/runner.tar.gz" -C "$base_home"
  chown -R "$runner_user:$runner_group" "$base_home"
fi
names=()
configure_runner() {
  local home=$1 name=$2 label=$3 registration_token
  install -d -m 700 -o "$runner_user" -g "$runner_group" "$home"
  if [[ ! -f "$home/.runner" ]]; then
    if [[ "$home" != "$base_home" ]]; then
      install -d "$home/bin" "$home/externals"
      cp -a "$base_home/bin/." "$home/bin/"
      cp -a "$base_home/externals/." "$home/externals/"
      for source in "$base_home"/*.sh "$base_home"/*.sh.template "$base_home"/*.cmd.template; do
        [[ $(basename "$source") == svc.sh ]] && continue
        [[ -f "$source" ]] && cp -a "$source" "$home/"
      done
      chown -R "$runner_user:$runner_group" "$home"
    fi
    registration_token=$(gh api --method POST "repos/$repo/actions/runners/registration-token" --jq .token)
    (cd "$home"; runuser -u "$runner_user" -- env -i HOME="$home" PATH=/usr/local/bin:/usr/bin:/bin ./config.sh --unattended --url "https://github.com/$repo" --token "$registration_token" --name "$name" --labels "$label" --work _work)
    unset registration_token
  fi
  grep -Fq "$name" "$home/svc.sh" || { printf 'Service name mismatch in %s; stopped.\n' "$home" >&2; exit 1; }
  (cd "$home"; if [[ ! -f .service ]]; then ./svc.sh install "$runner_user"; fi; ./svc.sh start)
  names+=("$name")
}
for ((slot=1;slot<=collectors;slot++)); do
  home=$base_home; name=findmyrom-metadata-103
  if ((slot>1)); then home="/opt/findmyrom-actions-metadata-$slot"; name="findmyrom-metadata-103-$slot"; fi
  configure_runner "$home" "$name" findmyrom-metadata
done
configure_runner /opt/findmyrom-actions-publisher findmyrom-publisher-103 findmyrom-publisher
for attempt in {1..12}; do
  gh api "repos/$repo/actions/runners" > "$tempdir/runners.json"
  if python3 - "$tempdir/runners.json" "${names[@]}" <<'PY'
import json,sys
rows={x['name']:x for x in json.load(open(sys.argv[1]))['runners']}
raise SystemExit(0 if all(rows.get(name,{}).get('status')=='online' for name in sys.argv[2:]) else 1)
PY
  then break; fi
  if ((attempt==12)); then printf '%s\n' 'Some runner slots are not online; routing has not changed.' >&2; exit 1; fi
  sleep 5
done
# Route the publisher separately so catalogue backlogs cannot occupy its slot.
gh variable set FMR_CATALOGUE_STATE_DIR --repo "$repo" --body "$state_home"
gh variable set FMR_PUBLISH_RUNNER --repo "$repo" --body '["self-hosted","findmyrom-publisher"]'
gh variable set FMR_CATALOGUE_RUNNER --repo "$repo" --body '["self-hosted","findmyrom-metadata"]'
gh workflow run catalogue_bootstrap.yml --repo "$repo" --ref main
printf 'Ready: %s catalogue slots + 1 dedicated D1 publisher; shared local checkpoints.\n' "$collectors"
printf '%s\n' "Status: https://github.com/$repo/actions/workflows/catalogue_bootstrap.yml"
