"""Run once under the existing venv; systemd supplies the ten-minute schedule.

Only the fixed repository/path is watched. No GitHub token, extraction secrets,
proxy configuration, package installation or firmware downloads are required.
"""
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

REPO = 'TunJinshou050608/findmyrom-runner'
SCRIPT = 'deploy/extractor/update-preparation.sh'
API = f'https://api.github.com/repos/{REPO}/commits?sha=main&path={SCRIPT}&per_page=1'
STATE = Path('/var/lib/fmr-extractor-update')
LOG = logging.getLogger('fmr-extractor-update')


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise ValueError('Unexpected update redirect')


def fetch(url, etag=None):
    headers = {'User-Agent': 'FindMyROM-extractor-updater', 'Accept-Encoding': 'identity'}
    if etag:
        headers['If-None-Match'] = etag
    request = Request(url, headers=headers)
    # Never inherit HTTP(S)_PROXY/ALL_PROXY or the host proxy configuration.
    opener = build_opener(ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=20) as response:
            data = response.read(131073)
            if len(data) > 131072:
                raise ValueError('Update metadata or script exceeds size limit')
            if response.status != 200:
                raise ValueError('Unexpected update response')
            return data, response.headers.get('ETag')
    except HTTPError as error:
        if error.code == 304:
            return None, etag
        raise


def execute(script, commit):
    # A fresh process group allows a timeout to stop curl and all child processes.
    env = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'FMR_UPDATE_REF': commit}
    process = subprocess.Popen(['/bin/bash', str(script)], env=env, start_new_session=True)
    try:
        code = process.wait(timeout=300)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        raise RuntimeError('Update timed out; not recorded as applied')
    if code != 0:
        raise RuntimeError(f'Update failed with exit code {code}; will retry next check')


def write_state(directory, value):
    # Rename only after successful installation; a failed update remains retryable.
    temporary = directory / 'applied.json.new'
    with temporary.open('w') as handle:
        json.dump(value, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, directory / 'applied.json')


def check(directory=STATE, get=fetch, run=execute):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (directory / 'check.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            LOG.info('Another update check is running; skipped')
            return 'locked'
        state_file = directory / 'applied.json'
        state = json.loads(state_file.read_text()) if state_file.exists() else {}
        payload, etag = get(API, state.get('etag'))
        if payload is None:
            if not state.get('sha256'):
                raise ValueError('Conditional response without an applied update')
            LOG.info('Update script unchanged (HTTP 304)')
            return 'unchanged'
        records = json.loads(payload)
        if not isinstance(records, list) or not records or not isinstance(records[0], dict):
            raise ValueError('Invalid update history')
        commit = records[0].get('sha', '')
        if not isinstance(commit, str) or not re.fullmatch('[0-9a-f]{40}', commit):
            raise ValueError('Invalid update revision')
        if commit == state.get('commit'):
            write_state(directory, {**state, 'etag': etag})
            LOG.info('Update script unchanged (%s)', commit[:12])
            return 'unchanged'
        # Fetch script and its referenced modules at the same immutable commit.
        script, _ = get(f'https://raw.githubusercontent.com/{REPO}/{commit}/{SCRIPT}')
        if not script or not script.startswith(b'#!/bin/bash\n') or b'\x00' in script:
            raise ValueError('Invalid update script')
        digest = hashlib.sha256(script).hexdigest()
        if digest != state.get('sha256'):
            with tempfile.TemporaryDirectory(prefix='fmr-update-') as temporary:
                target = Path(temporary) / 'update.sh'
                target.write_bytes(script)
                os.chmod(target, 0o600)
                # Syntax check does not execute the newly downloaded script.
                subprocess.run(['/bin/bash', '-n', str(target)], check=True, timeout=10)
                LOG.info('Applying update %s (script SHA-256 %s)', commit[:12], digest)
                run(target, commit)
            outcome = 'applied'
        else:
            outcome = 'unchanged'
        write_state(directory, {'commit': commit, 'sha256': digest, 'etag': etag})
        LOG.info('Update check complete: %s (%s)', outcome, commit[:12])
        return outcome


def main():
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    if os.geteuid() != 0:
        raise SystemExit('Run using fmr-extractor-update.service')
    try:
        check()
    except Exception as error:
        LOG.error('Update check failed: %s. Installed version retained; retry at next timer.', error)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
