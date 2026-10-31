"""Finite, resumable cloud experiment. Detailed manifests stay in private D1.

Discovery and extraction code are pinned by the workflow tag and GITHUB_SHA.
Each continuation consumes a bounded batch; no recurring schedule is installed.
"""
import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
from urllib.parse import urlsplit
import httpx

from fmr_core.d1_client import D1Client
from fmr_ingest_stock.device_sync import sync_devices
from fmr_ingest_stock.firmware_sync import sync_firmwares, normalize
from fmr_ingest_stock.history import discover_device
from . import VaultClient, extract_partitions_via_range, run_extraction_batch
from .patcher import LIMITS
from verify_phase1_live import MeteredClient, verify_download

SITE = 'https://findmyrom.com'
KIND = 'history_sweep'
BATCH = 12


def require(value):
    if not value:
        raise ValueError('Sweep condition failed')


def save(db, key, value):
    encoded = json.dumps(value, sort_keys=True, separators=(',', ':'))
    require(len(encoded.encode()) <= 1500000)
    db.execute('''INSERT INTO crawler_sources(device_codename,source_kind,source_target,last_cursor_val,last_checked_at)
        VALUES ('__global__',?,?,?,?) ON CONFLICT(device_codename,source_kind,source_target)
        DO UPDATE SET last_cursor_val=excluded.last_cursor_val,last_checked_at=excluded.last_checked_at''',
        (KIND, key, encoded, int(time.time())))


def load(db, key, default=None):
    rows = db.query("SELECT last_cursor_val FROM crawler_sources WHERE device_codename='__global__' AND source_kind=? AND source_target=?", (KIND, key))
    return json.loads(rows[0]['last_cursor_val']) if rows else default


def case_key(run, token):
    return run + ':case:' + token



def load_cases(db, run):
    rows = db.query("SELECT source_target,last_cursor_val FROM crawler_sources WHERE device_codename='__global__' AND source_kind=? AND source_target GLOB ?", (KIND,run+':case:*'))
    return {r['source_target'].rsplit(':',1)[-1]:json.loads(r['last_cursor_val']) for r in rows}

def planned_rows(db, tokens):
    rows = []
    for start in range(0, len(tokens), 90):
        group = tokens[start:start+90]
        rows.extend(db.query('SELECT * FROM stock_firmwares WHERE public_token IN (' + ','.join('?' for _ in group) + ')', group))
    return rows


def discover(db, run, targets, ref, sha):
    require(1 <= len(targets) <= 8 and len({t['codename'] for t in targets}) == len(targets))
    control = {'run_id': run, 'ref': ref, 'sha': sha, 'generation': 0,
               'targets': targets, 'tokens': [], 'state': 'discovering', 'created_at': int(time.time())}
    save(db, run, control)
    for target in targets:
        code = target['codename']
        try:
            rows, manifest = discover_device(code)
        except Exception as exc:
            rows, manifest = [], {'errors':[{'stage':'discovery','error':type(exc).__name__}], 'conflicts':[], 'detail_pages':[], 'archive_pages':[]}
        manifest['records'] = len(rows)
        manifest['rows'] = rows
        save(db, run + ':source:' + code, manifest)
        device = {'codename': code, 'brand': 'Xiaomi', 'name': target['name'],
                  'android_release': min((int(float(r['android'])) for r in rows if r['android'] != 'unknown'), default=13)}
        with tempfile.TemporaryDirectory(prefix='history_metadata_') as temp:
            old = Path.cwd()
            try:
                os.chdir(temp)
                Path('history_devices.json').write_text(json.dumps([device]))
                Path('history_firmwares.json').write_text(json.dumps(rows))
                sync_devices(db, 'history_devices.json')
                stats = sync_firmwares(db, 'history_firmwares.json', priority_codenames={code})
                require(stats['firmwares_rejected'] == 0)
            finally:
                os.chdir(old)
        stored = db.query('SELECT public_token,device_codename,region_code,version_str,package_type FROM stock_firmwares WHERE device_codename=?', (code,))
        expected = {tuple(normalize(r, 'xiaomi')[k] for k in ('device_codename','region_code','version_str','package_type')) for r in rows}
        tokens = [r['public_token'] for r in stored if tuple(r[k] for k in ('device_codename','region_code','version_str','package_type')) in expected]
        require(len(tokens) == len(expected))
        control['tokens'].extend(tokens)
        require(len(control['tokens']) <= 10000)
        save(db, run, control)
    control['state'] = 'working'
    save(db, run, control)
    return control


class ScopedDB:
    """The real scheduler and SQL writes, limited to this frozen experiment."""
    def __init__(self, db, rows, codes):
        self.db, self.rows, self.codes = db, rows, codes

    def query(self, sql, params=()):
        if sql.startswith("SELECT * FROM stock_firmwares WHERE extract_status='pending'"):
            return [r for r in self.rows if r['extract_status'] == 'pending'][:params[0]]
        return self.db.query(sql, params)

    def execute(self, sql, params=(), **kwargs):
        if "extract_error='Stale processing lock timeout'" in sql:
            sql += ' AND device_codename IN (' + ','.join('?' for _ in self.codes) + ')'
            params = tuple(params) + tuple(self.codes)
        return self.db.execute(sql, params, **kwargs)

    def execute_batch(self, statements):
        return self.db.execute_batch(statements)


def link_check(client, url):
    # A 200 is a live link but not a Range-capable extraction source. Its body
    # is never consumed. 206 consumes at most the requested one byte.
    with client.stream('GET', url, headers={'Range': 'bytes=0-0', 'Accept-Encoding': 'identity'}) as response:
        status = response.status_code
        if status == 206:
            require(re.fullmatch(r'bytes 0-0/\d+', response.headers.get('Content-Range', '')) is not None)
            data = bytearray()
            for chunk in response.iter_raw(65536):
                data.extend(chunk)
                require(len(data) <= 1)
            require(len(data) == 1)
        return {'status': status, 'live': status in (200,206), 'range': status == 206}



def checked_link(client, url):
    for attempt in range(3):
        try:
            result = link_check(client, url)
            if result['status'] not in (429, 500, 502, 503, 504) or attempt == 2:
                return result
        except (httpx.TimeoutException, httpx.NetworkError):
            if attempt == 2:
                raise
        if os.environ.get('FMR_TEST_NO_SLEEP') != '1':
            time.sleep(attempt + 1)

def classify(exc):
    safe = {'Unsupported incremental payload operation': 'UNSUPPORTED_PAYLOAD_OPERATION',
            'No supported partitions or stored OTA payload': 'UNSUPPORTED_PACKAGE_LAYOUT',
            'Invalid partition image magic or size': 'INVALID_IMAGE',
            'Firmware download budget exceeded': 'BYTE_BUDGET',
            'Server ignored HTTP Range': 'NO_RANGE'}
    return safe.get(str(exc), type(exc).__name__)


def validate_delivery(client, db, row, case):
    token = row['public_token']
    page = client.get(SITE + '/verify/' + token)
    require(page.status_code == 200)
    require(row['original_official_url'] not in page.text and row['fast_cdn_url'] not in page.text)
    for line, target in [('1',row['fast_cdn_url']), ('2',row['original_official_url'])]:
        ticket = client.post(SITE + '/api/ticket', json={'token': token, 'line': line})
        require(ticket.status_code == 200)
        url = ticket.json()['download_url']
        require(url.startswith('/api/dl?'))
        response = client.get(SITE + url)
        require(response.status_code == 302 and response.headers.get('Location') == target)
    case['redirect_verified'] = True
    if row['package_type'] != 'recovery_zip':
        return
    images = db.query('SELECT public_token,partition_name,file_size_bytes,sha256_hash FROM extracted_boot_images WHERE firmware_id=? AND variant_type=\'stock\'', (row['id'],))
    actual = {r['partition_name'] for r in images}
    expected = set(case.get('inventory', {}).get('partitions', []))
    require(bool(actual & {'boot','init_boot'}) and expected and actual == expected)
    device = client.get(SITE + '/devices/' + row['device_codename'], params={'ver': row['version_str']})
    require(device.status_code == 200 and token in device.text)
    require(all(image['public_token'] in device.text for image in images))
    for image in images:
        page = client.get(SITE + '/verify/' + image['public_token'])
        require(page.status_code == 200 and image['sha256_hash'] in page.text)
        require(row['original_official_url'] not in page.text and row['fast_cdn_url'] not in page.text)
        ticket = client.post(SITE + '/api/ticket', json={'token': image['public_token']})
        require(ticket.status_code == 200)
        url = ticket.json()['download_url']
        hit = verify_download(client, url, image)
        for _ in range(3):
            if hit:
                break
            time.sleep(2)
            hit = verify_download(client, url, image)
        require(hit)
    case.update(images_verified=True, partitions=sorted(actual))


def choose_batch(rows, cases, codes):
    pools = {code: [] for code in codes}
    for row in sorted(rows, key=lambda r: (r.get('published_date') or '', r['version_str']), reverse=True):
        if not cases.get(row['public_token'], {}).get('final'):
            pools[row['device_codename']].append(row)
    batch = []
    while len(batch) < BATCH and any(pools.values()):
        for code in codes:
            if pools[code] and len(batch) < BATCH:
                batch.append(pools[code].pop(0))
    return batch


def work(db, control):
    run = control['run_id']
    codes = [t['codename'] for t in control['targets']]
    rows = planned_rows(db, control['tokens'])
    require(len(rows) == len(control['tokens']))
    cases = load_cases(db,run)
    batch = choose_batch(rows, cases, codes)
    require(control['generation'] <= len(rows) * 4 + 1)
    # Recover only this run's abandoned claims; a different experiment's queue
    # is never fed into the extraction scheduler.
    now = int(time.time())
    for row in batch:
        if row['extract_status'] == 'processing' and row['updated_at'] <= now - 1800:
            retries = row['extract_retries'] + 1
            status = 'failed' if retries >= 3 else 'pending'
            db.execute("UPDATE stock_firmwares SET extract_status=?,extract_retries=?,extract_error='Stale processing lock timeout',updated_at=? WHERE id=? AND extract_status='processing' AND updated_at<=?", (status,retries,now,row['id'],now-1800))
            row.update(extract_status=status, extract_retries=retries)
    with httpx.Client(timeout=30, follow_redirects=False) as client:
        for row in batch:
            token = row['public_token']
            case = cases.setdefault(token, {})
            case['runs'] = case.get('runs', 0) + 1
            if case['runs'] > 4:
                case.update(final=True, ok=False, error='CASE_ATTEMPTS_EXHAUSTED')
                save(db, case_key(run,token), case)
                continue
            try:
                case['fast_link'] = checked_link(client, row['fast_cdn_url'])
                case['official_link'] = checked_link(client, row['original_official_url'])
            except Exception as exc:
                case['link_error'] = classify(exc)
            save(db, case_key(run,token), case)
        selected = [r for r in batch if not cases[r['public_token']].get('final')]
        # Each firmware receives its own byte meter and package inventory. The
        # extractor, validation, upload, signing and coordinator remain SP03.
        by_url = {r['fast_cdn_url']: r for r in selected}
        def extractor(url, directory, deadline=None):
            row = by_url[url]
            case = cases[row['public_token']]
            metrics = {'range_bytes': 0, 'range_requests': 0}
            inventory = {}
            try:
                with MeteredClient(metrics, timeout=30, follow_redirects=False) as ranged:
                    result = extract_partitions_via_range(url, directory, http_client=ranged,
                                                          deadline=deadline, inventory=inventory)
                require(set(result) == set(inventory.get('partitions', [])))
                return result
            except Exception as exc:
                case['extraction_error'] = classify(exc)
                raise
            finally:
                case.update(inventory=inventory, metrics=metrics)
        with VaultClient() as vault:
            stats = run_extraction_batch(ScopedDB(db, selected, codes), vault, extractor,
                batch_limit=BATCH, max_workers=2, max_runtime_sec=3000)
        control['last_batch'] = stats
        current = {r['public_token']: r for r in planned_rows(db, [r['public_token'] for r in selected])}
        for old in selected:
            token = old['public_token']
            row, case = current[token], cases[token]
            if row['extract_status'] in ('pending','processing') and not stats['circuit_broken']:
                # No false "done": transient retries and unfinished claims are
                # resumed by the next job. All attempts remain finite.
                case['final'] = False
            elif row['extract_status'] == 'failed':
                case.update(final=True, ok=False, error=case.get('extraction_error') or row['extract_error'])
            else:
                try:
                    validate_delivery(client, db, row, case)
                    case.update(final=True, ok=bool(case.get('fast_link',{}).get('live') and case.get('official_link',{}).get('live')))
                    if row['package_type'] != 'recovery_zip':
                        case['extraction'] = 'FASTBOOT_NOT_RANDOM_ACCESS_SUPPORTED'
                except Exception as exc:
                    case.update(final=case['runs'] >= 4, ok=False, error='DELIVERY_' + type(exc).__name__)
            save(db, case_key(run,token), case)
        if stats['circuit_broken']:
            control['state'] = 'blocked_auth'
            save(db, run, control)
            return False
    remaining = sum(not cases.get(r['public_token'],{}).get('final') for r in rows)
    control['remaining'] = remaining
    control['generation'] += 1
    control['state'] = 'working' if remaining else 'finished'
    save(db, run, control)
    return remaining > 0


def report(db, control):
    run = control['run_id']
    rows = planned_rows(db, control['tokens'])
    summary = {'run_id': run, 'state': control['state'], 'generation': control['generation'], 'devices': []}
    summary['missing_catalogue_rows'] = max(0,len(control['tokens'])-len(rows))
    all_cases = load_cases(db,run)
    complete = control['state'] == 'finished' and not summary['missing_catalogue_rows']
    for target in control['targets']:
        code = target['codename']
        source = load(db, run + ':source:' + code, {})
        local = [r for r in rows if r['device_codename'] == code]
        cases = {r['public_token']: all_cases.get(r['public_token'],{}) for r in local}
        versions = {(r['region_code'],r['version_str']) for r in local}
        verified_versions = {(r['region_code'],r['version_str']) for r in local if cases[r['public_token']].get('images_verified') and cases[r['public_token']].get('ok')}
        counters = Counter(r['extract_status'] for r in local)
        errors = Counter(c.get('error') or c.get('extraction_error') for c in cases.values() if c.get('error') or c.get('extraction_error'))
        item = {'codename': code, 'source_pages': source.get('discovered_pages',0),
                'source_errors': len(source.get('errors',[])), 'rejected_records': source.get('rejected_records',0),
                'alternate_packages_not_published': len(source.get('conflicts',[])),
                'catalog_entries': len(local), 'missing_catalogue_rows': max(0,source.get('records',len(local))-len(local)), 'system_region_versions': len(versions),
                'versions_with_verified_images': len(verified_versions),
                'missing_image_versions': len(versions-verified_versions),
                'links_and_redirects_verified': sum(bool(c.get('ok') and c.get('redirect_verified',False)) for c in cases.values()),
                'verified_image_files': sum(len(c.get('partitions',[])) for c in cases.values() if c.get('images_verified')),
                'range_response_body_bytes': sum(c.get('metrics',{}).get('range_bytes',0) for c in cases.values()),
                'unfinished': sum(not c.get('final') for c in cases.values()),
                'extraction_states': dict(counters), 'errors': dict(errors)}
        item['complete'] = bool(local) and not (item['missing_catalogue_rows'] or item['source_errors'] or item['rejected_records'] or item['alternate_packages_not_published'] or item['missing_image_versions'] or item['unfinished']) and item['links_and_redirects_verified'] == len(local)
        complete = complete and item['complete']
        summary['devices'].append(item)
    summary['all_requested_coverage_passed'] = complete
    save(db, run + ':report', summary)
    print(json.dumps(summary, sort_keys=True))
    path = os.environ.get('GITHUB_STEP_SUMMARY')
    if path:
        with open(path,'a') as handle:
            handle.write('## Frozen history sweep — '+('IN PROGRESS; NOT A PASS' if control['state'] == 'working' else 'PASS' if complete else 'STOPPED / COVERAGE GAPS')+'\n\n```json\n'+json.dumps(summary,indent=2)+'\n```\n\nDetailed source records and per-version failures are retained in private D1; no ROM artifacts are public.\n')
    return summary


def dispatch(control):
    repository = os.environ['GITHUB_REPOSITORY']
    require(re.fullmatch('[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',repository) is not None)
    with httpx.Client(timeout=30, follow_redirects=False) as client:
        response = client.post('https://api.github.com/repos/'+repository+'/actions/workflows/history_sweep.yml/dispatches',
            headers={'Authorization':'Bearer '+os.environ['GH_WORKFLOW_TOKEN'],
                     'Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28',
                     'User-Agent':'FindMyROM-History'},
            json={'ref':control['ref'], 'inputs':{'run_id':control['run_id'],'generation':str(control['generation'])}})
        require(response.status_code in (204, 200))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-id',default='')
    parser.add_argument('--generation',type=int,default=0)
    args = parser.parse_args(argv)
    db, control = None, None
    try:
        require(os.environ.get('GITHUB_ACTIONS') == 'true' and os.environ.get('GITHUB_REF_TYPE') == 'tag')
        run = args.run_id or 'hs_' + os.environ['GITHUB_RUN_ID']
        require(re.fullmatch('[A-Za-z0-9_-]{3,60}',run) is not None)
        db = D1Client(cf_account_id=os.environ['CF_ACCOUNT_ID'],cf_api_token=os.environ['CF_API_TOKEN'],cf_database_id=os.environ['CF_D1_DATABASE_ID'])
        if not args.run_id:
            require(load(db,run) is None)
            config = json.loads(os.environ['FMR_HISTORY_CONFIG'])
            control = discover(db,run,config['targets'],os.environ['GITHUB_REF_NAME'],os.environ['GITHUB_SHA'])
        else:
            control = load(db,run)
            require(control is not None and control['sha'] == os.environ['GITHUB_SHA'] and control['ref'] == os.environ['GITHUB_REF_NAME'])
            if args.generation != control['generation']:
                print(json.dumps({'duplicate_or_stale_dispatch':True,'run_id':run}))
                return 0
        again = work(db,control)
        summary = report(db,control)
        if again:
            dispatch(control)
            print(json.dumps({'continuation_queued':True,'run_id':run,'generation':control['generation']}))
        # A running experiment is not a pass. Terminal partial results fail the
        # final Actions run, even though healthy resources remain published.
        return 0 if again or summary['all_requested_coverage_passed'] else 1
    except Exception as exc:
        print(json.dumps({'error_type':type(exc).__name__,'run_id':control.get('run_id') if control else None}))
        if db and control is None and 'run' in locals():
            control = load(db,run)
        if db and control:
            control['state'] = 'blocked_error'
            save(db,control['run_id'],control)
            report(db,control)
        return 1
    finally:
        if db:
            db.close()


if __name__ == '__main__':
    raise SystemExit(main())
