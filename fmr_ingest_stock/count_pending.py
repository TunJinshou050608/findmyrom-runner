"""Count local catalogue records that still need a D1 publish attempt.

This is read-only with respect to D1. It merges the per-shard ledgers written
by parallel drain runs into the main local publisher ledger, then counts the
remaining candidates from the local vendor snapshots.
"""
import json
import os
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path

from .catalogue_publish import candidates, open_ledger
from .staged_catalogue import ROOT, open_local
from .vendor_catalogue import SUPPRESSED_VENDORS, VENDORS


def merge_shard_ledgers(main):
    merged = 0
    for shard in sorted(ROOT.glob('shards/*/parallel-*/publisher.sqlite')):
        try:
            with sqlite3.connect(main) as conn:
                conn.execute('ATTACH DATABASE ? AS shard', (str(shard),))
                before = conn.total_changes
                conn.execute('INSERT OR REPLACE INTO published SELECT vendor,key,hash FROM shard.published')
                merged += conn.total_changes - before
                conn.commit()
        except sqlite3.Error as error:
            print(json.dumps({'event': 'shard_ledger_skipped', 'path': str(shard), 'error': type(error).__name__}), flush=True)
    return merged


def tg_status():
    tg = Path(os.getenv('FMR_TG_STATE_DIR', str(Path(ROOT) / 'tg-v4')))
    latest_path = tg / 'clean/audits/full-quality-latest.json'
    if not latest_path.exists():
        return {'status': 'no_tg_audit', 'path': str(latest_path)}
    latest = json.loads(latest_path.read_text())
    run_id = str(latest.get('audit_run_id'))
    out = tg / 'clean/audits' / ('full-quality-' + run_id)
    report = latest
    if (out / 'report.json').exists():
        report = json.loads((out / 'report.json').read_text())
    publication = None
    if (out / 'publication_result.json').exists():
        publication = json.loads((out / 'publication_result.json').read_text())
    completed = None
    if report.get('completed_at'):
        completed = datetime.fromisoformat(report['completed_at'].replace('Z', '+00:00')).timestamp()
    workers = []
    for index in range(4):
        path = tg / 'workers' / str(index) / 'search-v3/search.sqlite'
        if not path.exists():
            continue
        with sqlite3.connect(path) as db:
            total = db.execute('SELECT count(*) FROM tg_search_records').fetchone()[0]
            new = db.execute('SELECT count(*) FROM tg_search_records WHERE updated>?', (completed,)).fetchone()[0] if completed else None
            new_accepted = db.execute('SELECT count(*) FROM tg_search_records WHERE updated>? AND accepted=1', (completed,)).fetchone()[0] if completed else None
        workers.append({'worker': index, 'total': total, 'new_since_audit': new, 'new_accepted_since_audit': new_accepted})
    clean_total = None
    clean_path = tg / 'clean/search-v3/search.sqlite'
    if clean_path.exists():
        with sqlite3.connect(clean_path) as db:
            clean_total = db.execute('SELECT count(*) FROM tg_search_records').fetchone()[0]
    return {
        'audit_run_id': run_id,
        'qualified': report.get('qualified'),
        'checked': report.get('all_records_checked'),
        'rejected_or_incomplete': report.get('rejected_or_incomplete'),
        'published_and_verified': (publication or {}).get('published_and_verified'),
        'd1_rows_upserted': (publication or {}).get('D1_rows_upserted'),
        'excluded_missing_device_or_quality': (publication or {}).get('excluded_missing_production_device_or_quality'),
        'clean_total': clean_total,
        'workers': workers,
        'new_since_audit': sum(w['new_since_audit'] or 0 for w in workers),
        'new_accepted_since_audit': sum(w['new_accepted_since_audit'] or 0 for w in workers),
    }


def tg_key_diff():
    tg = Path(os.getenv('FMR_TG_STATE_DIR', str(Path(ROOT) / 'tg-v4')))
    latest_path = tg / 'clean/audits/full-quality-latest.json'
    clean_path = tg / 'clean/search-v3/search.sqlite'
    if not latest_path.exists() or not clean_path.exists():
        return {'status': 'missing_state'}
    latest = json.loads(latest_path.read_text())
    run_id = str(latest.get('audit_run_id'))
    decisions_path = tg / 'clean/audits' / ('full-quality-' + run_id) / 'decisions.json'
    old_keys = set()
    if decisions_path.exists():
        old_keys = {item['key'] for item in json.loads(decisions_path.read_text()).get('decisions', [])}
    with sqlite3.connect(clean_path) as db:
        new_keys = {row[0] for row in db.execute('SELECT key FROM tg_search_records')}
    return {
        'previous_audit_run_id': run_id,
        'previous_decision_keys': len(old_keys),
        'current_clean_keys': len(new_keys),
        'added_keys_since_previous_audit': len(new_keys - old_keys),
        'removed_keys_since_previous_audit': len(old_keys - new_keys),
    }


def main():
    main = Path(ROOT) / 'publisher.sqlite'
    if not main.exists():
        print(json.dumps({'event': 'missing_publisher_state', 'path': str(main)}), flush=True)
        return 2
    merged = merge_shard_ledgers(main)
    ledger = open_ledger(main)
    total = Counter()
    report = {}
    try:
        for vendor in VENDORS:
            if vendor in SUPPRESSED_VENDORS:
                continue
            path = Path(ROOT) / (vendor + '.sqlite')
            if not path.exists():
                report[vendor] = {'missing': True}
                continue
            local = open_local(path, initialize=False)
            try:
                counts = Counter(stage for stage, *_ in candidates(local, ledger, vendor))
            finally:
                local.close()
            report[vendor] = {'release': counts['release'], 'model': counts['model']}
            total.update(counts)
    finally:
        ledger.close()
    print(json.dumps({
        'event': 'pending_count',
        'merged_shard_ledger_rows': merged,
        'vendors': report,
        'release': total['release'],
        'model': total['model'],
        'total': total['release'] + total['model'],
        'tg': tg_status(),
        'tg_key_diff': tg_key_diff(),
    }, ensure_ascii=False, sort_keys=True), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
