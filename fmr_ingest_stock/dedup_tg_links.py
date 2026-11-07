"""Report duplicate TG records using their download-link sets.

The audit's qualified cohort and the fetcher's pending queue both carry the
original package (including download_links_json) in the local TG state. We
compare canonical link sets, then separate exact duplicates from records that
merely share a generic project download page.
"""
import argparse
import hashlib
import json
import re
import sqlite3
from collections import defaultdict
from pathlib import Path

import os


def norm_link(value):
    value = str(value or '').strip().replace('&amp;', '&')
    value = re.sub(r'/+$', '', value)
    return value


def link_set(package):
    raw = package.get('download_links_json')
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = []
    if not isinstance(raw, list):
        raw = []
    return tuple(sorted({norm_link(item) for item in raw if isinstance(item, str) and item.startswith(('http://', 'https://'))}))


def text_hash(package, observed_text=None):
    value = package.get('raw_text_hash')
    if isinstance(value, str) and value:
        return value
    if observed_text:
        return hashlib.sha256(observed_text.encode()).hexdigest()
    return None


def package_fields(package):
    taxonomy = package.get('taxonomy') or {}
    project = taxonomy.get('project') or {}
    versions = taxonomy.get('versions') or {}
    devices = taxonomy.get('devices') or {}
    maintenance = taxonomy.get('maintenance') or {}
    return {
        'device': package.get('device_codename') or devices.get('codename'),
        'project': package.get('project_name') or project.get('name'),
        'version': package.get('build_version_tag') or versions.get('project_version'),
        'maintainer': package.get('maintainer_name') or maintenance.get('maintainer_id'),
        'post_url': package.get('tg_post_url') or package.get('source_post_url'),
    }


def record(source, key, package, observed_text=None, accepted=None):
    fields = package_fields(package)
    links = link_set(package)
    return {
        'source': source,
        'key': key,
        'accepted': accepted,
        'links': links,
        'text_hash': text_hash(package, observed_text),
        **fields,
    }


def load_state(tg):
    latest_path = tg / 'clean/audits/full-quality-latest.json'
    if not latest_path.exists():
        raise SystemExit('missing TG audit: ' + str(latest_path))
    latest = json.loads(latest_path.read_text())
    run_id = str(latest.get('audit_run_id'))
    audit_dir = tg / 'clean/audits' / ('full-quality-' + run_id)
    decisions = json.loads((audit_dir / 'decisions.json').read_text()).get('decisions', [])
    products = json.loads((audit_dir / 'qualified.json').read_text()).get('products', [])
    audited_keys = {item['key'] for item in decisions}
    logged = {item.get('key') for item in decisions if item.get('qualified')}
    qualified = []
    for product in products:
        package = product.get('package') or {}
        qualified.append(record('qualified', product.get('previous_key'), package, product.get('observed_text')))
    pending = []
    db_path = tg / 'clean/search-v3/search.sqlite'
    with sqlite3.connect(db_path) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute('SELECT key,accepted,payload FROM tg_search_records').fetchall()
    for row in rows:
        if row['key'] in audited_keys:
            continue
        try:
            package = json.loads(row['payload'])
        except ValueError:
            continue
        pending.append(record('pending', row['key'], package, accepted=bool(row['accepted'])))
    return latest, run_id, qualified, pending


def classify(records):
    groups = defaultdict(list)
    missing = 0
    for item in records:
        if not item['links']:
            missing += 1
            continue
        groups[item['links']].append(item)
    exact = []
    same_build = []
    shared_link = []
    for links, items in groups.items():
        if len(items) < 2:
            continue
        builds = defaultdict(list)
        for item in items:
            builds[(item['device'], item['project'], item['version'], item['maintainer'])].append(item)
        if len(builds) == 1:
            items2 = next(iter(builds.values()))
            hashes = {item['text_hash'] for item in items2 if item['text_hash']}
            if len(hashes) <= 1:
                exact.append((links, items2))
            else:
                same_build.append((links, items2))
        else:
            shared_link.append((links, items))
    return groups, missing, exact, same_build, shared_link


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    tg = Path(os.getenv('FMR_TG_STATE_DIR', '/var/lib/findmyrom/catalogue-v3/tg-v4'))
    latest, run_id, qualified, pending = load_state(tg)
    records = qualified + pending
    groups, missing, exact, same_build, shared_link = classify(records)
    exact_qualified = sum(1 for _, items in exact for item in items if item['source'] == 'qualified')
    exact_pending = sum(1 for _, items in exact for item in items if item['source'] == 'pending')
    same_build_extra = sum(len(items) - 1 for _, items in same_build)
    shared_extra = sum(len(items) - 1 for _, items in shared_link)
    summary = {
        'audit_run_id': run_id,
        'qualified': len(qualified),
        'pending': len(pending),
        'pending_accepted': sum(1 for item in pending if item['accepted']),
        'with_links': len(records) - missing,
        'missing_links': missing,
        'link_groups': sum(1 for items in groups.values() if len(items) > 1),
        'exact_duplicate_groups': len(exact),
        'exact_extra_records': sum(len(items) - 1 for _, items in exact),
        'exact_qualified_records': exact_qualified,
        'exact_pending_records': exact_pending,
        'same_build_groups': len(same_build),
        'same_build_extra_records': same_build_extra,
        'shared_link_groups': len(shared_link),
        'shared_link_extra_records': shared_extra,
    }
    out = Path(args.output)
    with out.open('w') as handle:
        handle.write('type\tsource\tkey\taccepted\tdevice\tproject\tversion\tmaintainer\ttext_hash\tpost_url\tlinks\n')
        for kind, collection in (('exact', exact), ('same_build', same_build), ('shared_link', shared_link)):
            for links, items in collection:
                for item in items:
                    handle.write('\t'.join(str(value or '') for value in (
                        kind, item['source'], item['key'], item['accepted'], item['device'], item['project'],
                        item['version'], item['maintainer'], item['text_hash'], item['post_url'],
                        ' '.join(links),
                    )) + '\n')
    print(json.dumps({'event': 'tg_link_dedup', **summary, 'output': str(out)}, ensure_ascii=False, sort_keys=True), flush=True)
    if args.apply:
        raise SystemExit('apply mode not implemented; review the TSV first')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
