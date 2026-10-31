"""Manual cloud-only credential probe. Uses tiny diagnostic text, never firmware.

No credential values, resource URLs, tokens, Vault IDs or response bodies are logged.
Only this invocation's random records and draft Release are deleted on completion.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import sys
import time
import uuid

import httpx

from fmr_core.crypto import generate_public_token, sign_boot_image, sign_firmware
from fmr_core.d1_client import D1Client

SITE = 'https://findmyrom.com'


def require(condition: bool) -> None:
    if not condition:
        raise RuntimeError('Probe check failed')


def main() -> int:
    checks: dict[str, bool] = {}
    stage = 'configuration'
    db = None
    release_id = None
    fw, bt = generate_public_token('fw'), generate_public_token('bt')
    tag = 'fmr-credential-probe-' + uuid.uuid4().hex
    client = httpx.Client(timeout=30, follow_redirects=False)
    headers: dict[str, str] = {}
    repo = ''
    cleanup_ok = True
    try:
        require(os.environ.get('GITHUB_ACTIONS') == 'true')
        values = {key: os.environ[key].strip() for key in (
            'CF_ACCOUNT_ID', 'CF_API_TOKEN', 'CF_D1_DATABASE_ID',
            'GH_VAULT_REPO', 'GH_VAULT_TOKEN', 'SIGN_PRIVATE_KEY_HEX')}
        require(all(values.values()))
        repo = values['GH_VAULT_REPO']
        require(len(repo.split('/')) == 2 and all(
            c.isalnum() or c in '_-./' for c in repo))
        headers = {'Authorization': 'Bearer ' + values['GH_VAULT_TOKEN'],
                   'Accept': 'application/vnd.github+json',
                   'X-GitHub-Api-Version': '2022-11-28'}
        stage = 'd1_read'
        db = D1Client(cf_account_id=values['CF_ACCOUNT_ID'],
                      cf_api_token=values['CF_API_TOKEN'],
                      cf_database_id=values['CF_D1_DATABASE_ID'])
        require(db.query('SELECT 1 AS ok')[0]['ok'] == 1)
        checks[stage] = True
        stage = 'vault_private'
        response = client.get(f'https://api.github.com/repos/{repo}', headers=headers)
        require(response.status_code == 200 and response.json().get('private') is True)
        checks[stage] = True
        stage = 'vault_write'
        response = client.post(f'https://api.github.com/repos/{repo}/releases', headers=headers,
                               json={'tag_name': tag, 'name': 'Temporary credential probe',
                                     'body': 'Diagnostic text only. Not firmware. Automatically removed.',
                                     'draft': True})
        require(response.status_code == 201)
        release_id = response.json()['id']
        payload = gzip.compress(b'FindMyROM credential probe. NOT FIRMWARE. NOT FOR FLASHING.\n', mtime=0)
        digest = hashlib.sha256(payload).hexdigest()
        response = client.post(f'https://uploads.github.com/repos/{repo}/releases/{release_id}/assets',
                               params={'name': f'blob_{digest[:20]}.bin'},
                               headers={**headers, 'Content-Type': 'application/gzip'}, content=payload)
        require(response.status_code == 201)
        asset_id = response.json()['id']
        require(isinstance(asset_id, int) and asset_id > 0)
        checks[stage] = True
        stage = 'd1_write'
        brand = db.query('SELECT id FROM brands ORDER BY id LIMIT 1')
        require(bool(brand))
        db.execute("INSERT OR IGNORE INTO devices(codename,brand_id,marketing_name) VALUES ('__global__',?,'Global System Anchor')", (brand[0]['id'],))
        source = 'https://example.invalid/credential-probe.zip'
        db.execute('''INSERT INTO stock_firmwares(public_token,device_codename,region_code,
            version_str,android_version,package_type,original_official_url,fast_cdn_url,
            ed25519_signature,extract_status) VALUES (?,'__global__','Diagnostic',?,'unknown',
            'recovery_zip',?,?,?,'skipped')''',
            (fw, tag, source, source, sign_firmware(fw, source, values['SIGN_PRIVATE_KEY_HEX'])))
        firmware_id = db.query('SELECT id FROM stock_firmwares WHERE public_token=?', (fw,))[0]['id']
        db.execute('''INSERT INTO extracted_boot_images(public_token,firmware_id,device_codename,
            version_str,partition_name,variant_type,file_name,file_size_bytes,sha256_hash,
            gh_private_repo,gh_release_tag,gh_asset_id,ed25519_signature)
            VALUES (?,?,'__global__',?,'boot','stock','credential_probe_NOT_FOR_FLASHING.txt.gz',?,?,?,?,?,?)''',
            (bt, firmware_id, tag, len(payload), digest, repo, tag, asset_id,
             sign_boot_image(bt, digest, repo, asset_id, values['SIGN_PRIVATE_KEY_HEX'])))
        checks[stage] = True
        stage = 'worker_signature'
        response = client.get(f'{SITE}/verify/{bt}')
        require(response.status_code == 200)
        checks[stage] = True
        stage = 'ticket'
        response = client.post(f'{SITE}/api/ticket', json={'token': bt})
        require(response.status_code == 200)
        download = response.json()['download_url']
        require(download.startswith('/api/dl?') and len(download) < 1024)
        checks[stage] = True
        stage = 'asset_download'
        for attempt in range(6):
            with client.stream('GET', SITE + download) as response:
                require(response.status_code == 200)
                data = b''
                for chunk in response.iter_bytes():
                    data += chunk
                    require(len(data) <= 1024 * 1024)
                require(data == payload and hashlib.sha256(data).hexdigest() == digest)
                require(response.headers.get('X-FindMyROM-SHA256') == digest)
                cache_hit = response.headers.get('X-FindMyROM-Cache') == 'HIT'
            checks['asset_download'] = True
            if cache_hit:
                checks['cache_hit'] = True
                break
            time.sleep(2)
        stage = 'cache_hit'
        require(checks.get(stage, False))
    except Exception as exc:
        # Exception text/HTTP response bodies may contain sensitive URLs.
        print(json.dumps({'failed_stage': stage, 'error_type': type(exc).__name__}))
    finally:
        if db is not None:
            try:
                db.execute_batch([
                    ('DELETE FROM extracted_boot_images WHERE public_token=?', (bt,)),
                    ('DELETE FROM stock_firmwares WHERE public_token=?', (fw,)),
                ])
                require(not db.query('SELECT id FROM extracted_boot_images WHERE public_token=?', (bt,)))
                require(not db.query('SELECT id FROM stock_firmwares WHERE public_token=?', (fw,)))
            except Exception:
                cleanup_ok = False
        if release_id is not None:
            try:
                response = client.delete(f'https://api.github.com/repos/{repo}/releases/{release_id}', headers=headers)
                require(response.status_code in (204, 404))
                response = client.get(f'https://api.github.com/repos/{repo}/releases/{release_id}', headers=headers)
                require(response.status_code == 404)
            except Exception:
                cleanup_ok = False
        client.close()
        checks['cleanup'] = cleanup_ok
    required = ('d1_read', 'vault_private', 'vault_write', 'd1_write', 'worker_signature',
                'ticket', 'asset_download', 'cache_hit', 'cleanup')
    checks['ok'] = all(checks.get(key, False) for key in required)
    print(json.dumps(checks, sort_keys=True))
    return 0 if checks['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
