#!/usr/bin/env bash
set -euo pipefail
# Run on the authorised Linux host as root; no personal credentials enter jobs.
repo=TunJinshou050608/findmyrom-runner
runner_user=findmyrom-actions
runner_home=/opt/findmyrom-actions
runner_name=findmyrom-metadata-103
if [[ $(id -u) != 0 || $(uname -s) != Linux || ! -d /run/systemd/system ]]; then
  printf '%s\n' 'Requires root on Linux with systemd; no changes made.' >&2; exit 1
fi
for tool in gh python3 git curl tar sha256sum runuser useradd systemctl; do command -v "$tool" >/dev/null || { printf 'Missing dependency: %s\n' "$tool" >&2; exit 1; }; done
python3 -c 'import sys; assert sys.version_info >= (3,11), "Python 3.11+ required"'
case $(uname -m) in x86_64) arch=x64;; aarch64|arm64) arch=arm64;; *) printf '%s\n' 'Unsupported architecture; stopped.' >&2; exit 1;; esac
if (( $(df -Pk /opt | awk 'NR==2 {print $4}') < 2097152 )); then printf '%s\n' 'Requires 2 GiB free in /opt.' >&2; exit 1; fi
gh auth status >/dev/null
gh api "repos/$repo" --jq '.permissions.admin' | python3 -c 'import sys; assert sys.stdin.read().strip()=="true", "Repository admin access required"'
if ! id "$runner_user" >/dev/null 2>&1; then useradd --system --create-home --home-dir "$runner_home" --shell /bin/bash "$runner_user"; fi
install -d -m 700 -o "$runner_user" -g "$(id -gn "$runner_user")" "$runner_home"
# Verify venv creation as the unprivileged job user before registering anything.
runuser -u "$runner_user" -- env -i HOME="$runner_home" PATH=/usr/local/bin:/usr/bin:/bin bash -s <<'CHECK'
set -e
checkdir=$(mktemp -d "$HOME/venv-check.XXXXXX")
trap 'rm -rf "$checkdir"' EXIT
python3 -m venv "$checkdir/venv"
"$checkdir/venv/bin/python" -m pip --version >/dev/null
CHECK
if [[ ! -f "$runner_home/.runner" ]]; then
  tempdir=$(mktemp -d); trap 'rm -rf "$tempdir"' EXIT
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
  tar -xzf "$tempdir/runner.tar.gz" -C "$runner_home"
  chown -R "$runner_user:$(id -gn "$runner_user")" "$runner_home"
  registration_token=$(gh api --method POST "repos/$repo/actions/runners/registration-token" --jq .token)
  (cd "$runner_home"; runuser -u "$runner_user" -- env -i HOME="$runner_home" PATH=/usr/local/bin:/usr/bin:/bin ./config.sh --unattended --url "https://github.com/$repo" --token "$registration_token" --name "$runner_name" --labels findmyrom-metadata --work _work)
  unset registration_token
fi
(cd "$runner_home"; if [[ ! -f .service ]]; then ./svc.sh install "$runner_user"; fi; ./svc.sh start)
for attempt in {1..12}; do
  online=$(gh api "repos/$repo/actions/runners" --jq ".runners[] | select(.name == \"$runner_name\") | .status")
  if [[ "$online" == online ]]; then break; fi
  sleep 5
done
[[ ${online:-} == online ]] || { printf '%s\n' 'Runner not online; workflow routing has not changed.' >&2; exit 1; }
# Set routing only after a successful registration. Leave TG and image jobs alone.
gh variable set FMR_CATALOGUE_RUNNER --repo "$repo" --body '["self-hosted","findmyrom-metadata"]'
gh workflow run catalogue_bootstrap.yml --repo "$repo" --ref main
printf '%s\n' 'Metadata Runner online. Official catalogue bootstrap dispatched.' "Status: https://github.com/$repo/actions/workflows/catalogue_bootstrap.yml"
