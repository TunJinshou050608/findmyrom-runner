"""Manual, cloud-only, one-firmware acceptance using encrypted source selection.

Never prints source records, origins, Vault IDs or download tickets. Real valid
records/assets are retained; failures are reported, never relabelled as success.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from urllib.parse import urlsplit

import httpx
import yaml

from fmr_core.d1_client import D1Client
from fmr_extractor import VaultClient, extract_partitions_via_range, run_extraction_batch
from fmr_extractor.range_extractor import RemoteZip, MAX_DOWNLOAD_BYTES_PER_FW
from fmr_ingest_stock.cdn_rewriter import rewrite_to_fast_cdn
from fmr_ingest_stock.device_sync import sync_devices
from fmr_ingest_stock.firmware_sync import sync_firmwares

SITE = 'https://findmyrom.com'


def require(value):
    if not value:
        raise ValueError('Acceptance condition failed')


def read_metadata(client, url):
    parsed = urlsplit(url)
    require(parsed.scheme == 'https' and parsed.hostname == 'raw.githubusercontent.com')
    require(re.fullmatch(r'/XiaomiFirmwareUpdater/miui-updates-tracker/[0-9a-f]{40}/data/(devices|latest)\.yml', parsed.path) is not None)
    require(not parsed.query and not parsed.fragment and not parsed.username and not parsed.port)
    data = bytearray()
    with client.stream('GET', url) as response:
        response.raise_for_status()
        for chunk in response.iter_bytes(65536):
            data.extend(chunk)
            require(len(data) <= 5 * 1024 * 1024)
    return yaml.safe_load(bytes(data))


def select_seed(devices, firmwares, config):
    code, source_code = config['codename'], config['source_codename']
    require(re.fullmatch('[a-z0-9-]{2,32}', code) is not None)
    require(source_code.split('_', 1)[0] == code)
    require(isinstance(devices, dict) and isinstance(firmwares, list))
    name = devices[source_code][0]
    require(isinstance(name, str) and 1 <= len(name) <= 128)
    matches = [r for r in firmwares if isinstance(r, dict)
               and r.get('codename') == source_code and r.get('version') == config['version']
               and r.get('method') == 'Recovery' and r.get('branch') == 'Stable']
    require(len(matches) == 1)
    row = dict(matches[0])
    require(row.get('md5') == config['expected_md5'] and re.fullmatch('[0-9a-f]{32}', row['md5']) is not None)
    url = urlsplit(row['link'])
    require(url.scheme == 'https' and url.hostname in {
        'bigota.d.miui.com', 'hugeota.d.miui.com', 'ultimota.d.miui.com',
        'ultimateota.d.miui.com', 'superota.d.miui.com'})
    require(url.path.rsplit('/', 1)[-1] == config['filename'] and config['filename'].endswith('.zip'))
    require('blockota' not in config['filename'].lower())
    row['date'] = str(row.get('date', ''))
    device = {'codename': code, 'brand': 'Xiaomi', 'name': name,
              'android_release': int(float(row['android']))}
    return device, row


class MeteredStream(httpx.SyncByteStream):
    def __init__(self, inner, metrics):
        self.inner, self.metrics = inner, metrics

    def __iter__(self):
        for chunk in self.inner:
            self.metrics['range_bytes'] += len(chunk)
            require(self.metrics['range_bytes'] <= MAX_DOWNLOAD_BYTES_PER_FW)
            yield chunk

    def close(self):
        self.inner.close()


class MeteredClient(httpx.Client):
    def __init__(self, metrics, **kwargs):
        super().__init__(**kwargs)
        self.metrics = metrics

    @contextmanager
    def stream(self, *args, **kwargs):
        with super().stream(*args, **kwargs) as response:
            self.metrics['range_requests'] += 1
            response.stream = MeteredStream(response.stream, self.metrics)
            yield response


def verify_download(client, url, image):
    require(url.startswith('/api/dl?') and len(url) < 1024)
    digest, size = hashlib.sha256(), 0
    with client.stream('GET', SITE + url) as response:
        require(response.status_code == 200)
        require(response.headers.get('content-type') == 'application/gzip')
        require(response.headers.get('X-FindMyROM-SHA256') == image['sha256_hash'])
        for chunk in response.iter_bytes(65536):
            size += len(chunk)
            require(size <= image['file_size_bytes'] and size <= 200 * 1024 * 1024)
            digest.update(chunk)
        require(size == image['file_size_bytes'] and digest.hexdigest() == image['sha256_hash'])
        return response.headers.get('X-FindMyROM-Cache') == 'HIT'


def main():
    checks, metrics = {}, {'range_bytes': 0, 'range_requests': 0}
    stage, db, vault = 'configuration', None, None
    try:
        require(os.environ.get('GITHUB_ACTIONS') == 'true')
        config = json.loads(os.environ['FMR_PHASE1_CONFIG'])
        with httpx.Client(timeout=30, follow_redirects=False) as client:
            stage = 'source_metadata'
            device, firmware = select_seed(read_metadata(client, config['devices_source']),
                                           read_metadata(client, config['firmwares_source']), config)
            checks[stage] = True
            stage = 'range_preflight'
            fast = rewrite_to_fast_cdn('xiaomi', firmware['link'])
            with MeteredClient(metrics, timeout=30, follow_redirects=False) as ranged:
                remote = RemoteZip(fast, ranged, MAX_DOWNLOAD_BYTES_PER_FW)
                metrics['archive_bytes'] = remote.size
            require(metrics['archive_bytes'] > 0)
            checks[stage] = True
            stage = 'signed_ingestion'
            db = D1Client(cf_account_id=os.environ['CF_ACCOUNT_ID'],
                          cf_api_token=os.environ['CF_API_TOKEN'],
                          cf_database_id=os.environ['CF_D1_DATABASE_ID'])
            with tempfile.TemporaryDirectory(prefix='phase1_metadata_') as temp:
                # Stable cursor names; raw metadata exists only on this cloud runner.
                old_cwd = Path.cwd()
                try:
                    os.chdir(temp)
                    Path('phase1_devices.json').write_text(json.dumps([device]))
                    Path('phase1_firmwares.json').write_text(json.dumps([firmware]))
                    ds = sync_devices(db, 'phase1_devices.json')
                    fs = sync_firmwares(db, 'phase1_firmwares.json', priority_codenames={device['codename']})
                finally:
                    os.chdir(old_cwd)
            require(ds['devices_upserted'] == 1 and fs['inserted_or_updated'] == 1 and fs['firmwares_rejected'] == 0)
            rows = db.query('''SELECT id,public_token,extract_status FROM stock_firmwares
                WHERE device_codename=? AND version_str=? AND model_code=? AND package_type='recovery_zip' ''',
                (device['codename'], firmware['version'], firmware['codename']))
            require(len(rows) == 1)
            fw = rows[0]
            checks[stage] = True
            stage = 'range_extraction_archive'
            require(not db.query("SELECT id FROM stock_firmwares WHERE extract_status='pending' AND id!=?", (fw['id'],)))
            if fw['extract_status'] != 'done':
                require(fw['extract_status'] == 'pending')
                vault = VaultClient()
                def tracked_extractor(url, directory, deadline=None):
                    with MeteredClient(metrics, timeout=30, follow_redirects=False) as ranged:
                        return extract_partitions_via_range(url, directory, http_client=ranged,
                            max_bytes=MAX_DOWNLOAD_BYTES_PER_FW-metrics['range_bytes'], deadline=deadline)
                stats = run_extraction_batch(db, vault, tracked_extractor,
                    batch_limit=1, max_workers=1, max_runtime_sec=900)
                print(json.dumps({'extraction': stats}, sort_keys=True))
                require(stats['selected'] == 1 and stats['succeeded'] == 1 and not stats['circuit_broken'])
                checks['extracted_this_run'] = True
            else:
                checks['extracted_this_run'] = False
            require(db.query('SELECT extract_status FROM stock_firmwares WHERE id=?', (fw['id'],))[0]['extract_status'] == 'done')
            images = db.query('''SELECT public_token,partition_name,file_name,file_size_bytes,sha256_hash
                FROM extracted_boot_images WHERE firmware_id=? AND variant_type='stock' ORDER BY partition_name''', (fw['id'],))
            require(bool({'boot', 'init_boot'} & {r['partition_name'] for r in images}))
            checks[stage] = True
            stage = 'public_views'
            paths = ['/', '/?q='+device['codename'], '/brands/xiaomi', '/devices/'+device['codename']]
            for path in paths:
                response = client.get(SITE + path)
                require(response.status_code == 200 and device['codename'] in response.text)
                require(firmware['link'] not in response.text and fast not in response.text)
            response = client.get(SITE + '/verify/' + fw['public_token'])
            require(response.status_code == 200)
            ticket = client.post(SITE + '/api/ticket', json={'token': fw['public_token']})
            require(ticket.status_code == 200)
            url = ticket.json()['download_url']
            require(url.startswith('/api/dl?'))
            redirected = client.get(SITE + url)
            require(redirected.status_code == 302 and redirected.headers.get('location') == fast)
            checks[stage] = True
            stage = 'download_hash_and_cache'
            for image in images:
                response = client.get(SITE + '/verify/' + image['public_token'])
                require(response.status_code == 200 and image['sha256_hash'] in response.text)
                require(firmware['link'] not in response.text and fast not in response.text)
                ticket = client.post(SITE + '/api/ticket', json={'token': image['public_token']})
                require(ticket.status_code == 200)
                url = ticket.json()['download_url']
                hit = verify_download(client, url, image)
                for _ in range(4):
                    if hit:
                        break
                    time.sleep(2)
                    hit = verify_download(client, url, image)
                require(hit)
            checks[stage] = True
            metrics['images_verified'] = len(images)
        checks['ok'] = True
    except Exception as exc:
        error = {'failed_stage': stage, 'error_type': type(exc).__name__}
        if isinstance(exc, httpx.HTTPStatusError):
            error['http_status'] = exc.response.status_code
        print(json.dumps(error))
        checks['ok'] = False
    finally:
        if vault:
            vault.close()
        if db:
            db.close()
    print(json.dumps({'checks': checks, 'metrics': metrics}, sort_keys=True))
    return 0 if checks.get('ok') else 1


if __name__ == '__main__':
    sys.exit(main())
