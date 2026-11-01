"""Read-only live source diagnostics. Public output contains aggregates only."""
from collections import Counter
import json
import os
from pathlib import Path
import sqlite3
import traceback
import httpx
from urllib.parse import urlsplit, parse_qs

from .oem_sources import MetadataHTTP, resolve_atlas, download_origin
from .download_refresh import expiring
from .vendor_catalogue import durable_download, problem


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
            samples.append(value['release'])
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
                probes.append({'resolution': resolved.get('resolution'),
                    'origin_host': urlsplit(resolved.get('download_url', '')).hostname,
                    'query_keys': sorted(parse_qs(urlsplit(resolved.get('download_url', '')).query)),
                    'kind': row.get('kind'),
                    'durable': durable_download(vendor, resolved)})
            except Exception as exc:
                probes.append({'error': problem(exc), 'kind': row.get('kind'),
                    'reason': str(exc) if type(exc) is ValueError else None,
                    'url_issues':getattr(exc,'catalogue_url_issues',None),
                    'trace':[{'file':Path(frame.filename).name,'line':frame.lineno,'function':frame.name}
                             for frame in traceback.extract_tb(exc.__traceback__)[-5:]]})
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
