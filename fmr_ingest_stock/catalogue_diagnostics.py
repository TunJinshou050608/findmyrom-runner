"""Read-only live source diagnostics. Public output contains aggregates only."""
from collections import Counter
import json
import os
import re
from pathlib import Path
import sqlite3
import traceback
import httpx
from urllib.parse import urlsplit, parse_qs

from .oem_sources import MetadataHTTP, resolve_atlas, download_origin
from .download_refresh import expiring
from .vendor_catalogue import durable_download, problem


def alternate_probe(row,http):
    """Check a documented public OTA API for an exact-build alternative."""
    from urllib.parse import urlencode
    model=re.search(r'(?<![A-Za-z0-9])(?:RMX|CPH)[0-9]{4,5}(?![0-9])',row.get('version',''))
    if not model:return {'state':'no_model_identity'}
    try:
        payload=json.loads(http._get('https://roms.danielspringer.at/api/ota.php?'+urlencode({'model':model[0]})))
        matches=[entry for entry in payload.get('releases',[]) if entry.get('version','').strip()==row['version'].strip()]
        kinds=Counter('static' if re.search(r'\.(?:zip|ozip)$',urlsplit(entry.get('source_url','')).path,re.I)
            else urlsplit(entry.get('source_url','')).path.rsplit('/',1)[-1] if re.fullmatch('[A-Za-z]{1,30}',urlsplit(entry.get('source_url','')).path.rsplit('/',1)[-1]) else 'other'
            for entry in matches)
        return {'matches':len(matches),'endpoint_types':dict(kinds)}
    except Exception as exc:return {'error':problem(exc)}


def inspect(vendor):
    root = Path(os.environ['FMR_CATALOGUE_STATE_DIR'])
    path = root / (vendor + '.sqlite')
    if not path.exists():
        return {'vendor': vendor, 'state': 'missing_local_state'}
    with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=30) as db:
        records = [(key, json.loads(body)) for key, body in db.execute(
            "SELECT source_target,last_cursor_val FROM crawler_sources WHERE source_kind=? AND source_target GLOB ?",
            ('oem_incremental_v2', 'v2:' + vendor + ':*'))]
    counts, errors, kinds, samples, ready = Counter(), Counter(), Counter(), [], {}
    for key, value in records:
        stage = key.split(':')[2]
        if stage not in ('model', 'release'):
            continue
        counts[stage + ':' + value.get('status', 'pending')] += 1
        if stage == 'release':
            kinds[value['release'].get('kind', 'unknown')] += 1
            if value.get('status')=='resolved' and durable_download(vendor,value['release']):
                ready.setdefault(value['release'].get('kind','unknown'),value['release'])
        if value.get('error'):
            errors[stage + ':' + value['error']] += 1
        if stage == 'release' and (value.get('status') in ('blocked', 'failed', 'retry') or expiring(value['release'].get('download_url',''))):
            if value.get('error')=='ValueError':samples.insert(0,value['release'])
            else:samples.append(value['release'])
    probes = []
    with_client = MetadataHTTP()
    try:
        # Different error/origin families get a probe; avoid hammering a catalogue.
        families = set()
        for row in samples:
            family = (row.get('kind'), urlsplit(row.get('download_url', '')).hostname, row.get('resolution'))
            if family in families:
                continue
            families.add(family)
            try:
                resolved = resolve_atlas(row, with_client) if row.get('detail_page') else row
                probe={'resolution': resolved.get('resolution'),
                    'origin_host': urlsplit(resolved.get('download_url', '')).hostname,
                    'query_keys': sorted(parse_qs(urlsplit(resolved.get('download_url', '')).query)),
                    'kind': row.get('kind'),
                    'resolver_observation':resolved.get('resolver_observation'),
                    'version_shape':{'length':len(row.get('version','')),'space':' ' in row.get('version',''),
                                     'non_ascii':not row.get('version','').isascii()},
                    'durable': durable_download(vendor, resolved)}
                if not probe['durable'] and resolved.get('resolver_observation',{}).get('host','').endswith(('allawnos.com','allawntech.com')):
                    probe['alternate_api']=alternate_probe(row,with_client)
                probes.append(probe)
            except Exception as exc:
                probe={'error': problem(exc), 'kind': row.get('kind'),
                    'reason': str(exc) if type(exc) is ValueError else None,
                    'url_issues':getattr(exc,'catalogue_url_issues',None),
                    'trace':[{'file':Path(frame.filename).name,'line':frame.lineno,'function':frame.name}
                             for frame in traceback.extract_tb(exc.__traceback__)[-5:]]}
                if type(exc).__name__=='DeviceAttestationRequired':probe['alternate_api']=alternate_probe(row,with_client)
                probes.append(probe)
            if len(probes) >= 3:
                break
    finally:
        with_client.close()
    smoke=[]
    with httpx.Client(timeout=15,follow_redirects=False) as client:
        for kind,row in list(ready.items())[:2]:
            url=row['download_url'];renewable=bool(row.get('refresh_source'))
            try:
                if not renewable and not download_origin(vendor,url):continue
                # GET only our tiny redirect handler; HEAD for an official file.
                # Neither request follows redirects or consumes firmware bytes.
                with client.stream('GET' if renewable else 'HEAD',url) as response:
                    target=urlsplit(response.headers.get('Location','')).hostname
                    smoke.append({'kind':kind,'refresh':renewable,'http_status':response.status_code,'redirect_host':target})
            except Exception as exc:smoke.append({'kind':kind,'error':problem(exc)})
    return {'vendor': vendor, 'counts': dict(counts), 'errors': dict(errors), 'kinds': dict(kinds), 'probes': probes,'download_smoke':smoke}


def main():
    for vendor in ('oneplus', 'oppo', 'realme', 'vivo', 'asus'):
        try:
            result = inspect(vendor)
        except Exception as exc:
            result = {'vendor': vendor, 'diagnostic_error': problem(exc)}
        print(json.dumps(result, sort_keys=True), flush=True)


if __name__ == '__main__':
    main()
