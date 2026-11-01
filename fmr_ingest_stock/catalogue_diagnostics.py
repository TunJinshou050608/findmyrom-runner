"""Read-only live source diagnostics. Public output contains aggregates only."""
from collections import Counter
import json
import os
from pathlib import Path
import sqlite3
from urllib.parse import urlsplit, parse_qs

from .oem_sources import MetadataHTTP, resolve_atlas
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
    counts, errors, kinds, samples = Counter(), Counter(), Counter(), []
    for key, value in records:
        stage = key.split(':')[2]
        if stage not in ('model', 'release'):
            continue
        counts[stage + ':' + value.get('status', 'pending')] += 1
        if stage == 'release':
            kinds[value['release'].get('kind', 'unknown')] += 1
        if value.get('error'):
            errors[stage + ':' + value['error']] += 1
        if stage == 'release' and value.get('status') in ('blocked', 'failed', 'retry'):
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
                    'reason': str(exc) if type(exc) is ValueError else None})
            if len(probes) >= 3:
                break
    finally:
        with_client.close()
    return {'vendor': vendor, 'counts': dict(counts), 'errors': dict(errors), 'kinds': dict(kinds), 'probes': probes}


def main():
    for vendor in ('oneplus', 'oppo', 'realme', 'vivo', 'asus'):
        try:
            result = inspect(vendor)
        except Exception as exc:
            result = {'vendor': vendor, 'diagnostic_error': problem(exc)}
        print(json.dumps(result, sort_keys=True), flush=True)


if __name__ == '__main__':
    main()
