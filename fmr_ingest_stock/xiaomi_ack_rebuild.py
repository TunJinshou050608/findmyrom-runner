"""Reconcile Xiaomi publication receipts against D1 using local replay."""
import argparse
from collections import Counter
from itertools import islice
import json
import time
from pathlib import Path

from fmr_core.d1_client import D1Client
from fmr_ingest_stock.catalogue_publish import XIAOMI_REVIEW_DELAY, XIAOMI_REVIEW_SQL, acknowledgement, candidates, open_ledger
from fmr_ingest_stock.catalogue_sync import FirmwareIdentityPending, build_version, package_kind, publish_release
from fmr_ingest_stock.d1_budget import BudgetDB
from fmr_ingest_stock.device_sync import ensure_base_brands_and_anchor
from fmr_ingest_stock.oem_sources import canonical_code
from fmr_ingest_stock.staged_catalogue import Artifacts, ROOT, encrypt_file, open_local

BATCH_SIZE = 80
READ_LIMIT = 250_000
CURSOR = "xiaomi_receipt_rebuild_cursor"


class ReadOnlyD1(BudgetDB):
    def _request_cf_with_retry(self, payload, is_safe_to_retry):
        if not payload.get('sql', '').lstrip().upper().startswith('SELECT '):
            raise RuntimeError('Receipt reconciliation only permits D1 SELECT queries')
        return super()._request_cf_with_retry(payload, is_safe_to_retry)


class ShadowDB(D1Client):
    def execute_batch(self, statements):
        if not self._get_sqlite_conn().in_transaction:
            return super().execute_batch(statements)
        return {'changes': sum(self.execute(sql, params)['changes'] for sql, params in statements), 'success': True}


def insert_rows(db, table, rows):
    if not rows:
        return
    columns = list(rows[0])
    # The table names and columns come from the fixed D1 schema.
    sql = f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})"
    db._get_sqlite_conn().executemany(sql, [tuple(row[col] for col in columns) for row in rows])


def firmware_keys(device, release):
    from fmr_ingest_stock.xiaomi_variants import platform_parts, version_key

    code = device.get('canonical_code') or canonical_code('xiaomi', device.get('codename') or device['upstream_id'])
    region = str(release.get('region') or 'Global').strip()[:64]
    variant = release.get('variant_id')
    parts = platform_parts(variant)
    editions = {variant, parts[0] if parts else variant}
    if not variant:
        editions.add('standard')
    kind = package_kind('xiaomi',release)
    return {(code,region,build_version(version_key(release['version'],edition)),kind)
            for edition in editions}


def candidate_snapshot(remote, shadow, items):
    conn = shadow._get_sqlite_conn()
    conn.execute('DELETE FROM firmware_metadata')
    conn.execute('DELETE FROM stock_firmwares')
    conn.execute("DELETE FROM devices WHERE codename!='__global__'")
    keys = sorted({key for _,value,_ in items for key in firmware_keys(value['device'],value['release'])})
    codes = sorted({key[0] for key in keys})
    insert_rows(shadow, 'devices', remote.query(
        'SELECT * FROM devices WHERE codename IN (SELECT value FROM json_each(?))',
        (json.dumps(codes),)))
    rows = remote.query("""SELECT f.*,m.metadata_json,m.content_hash
        FROM json_each(?) AS wanted
        CROSS JOIN stock_firmwares f ON f.device_codename=json_extract(wanted.value,'$[0]')
            AND f.region_code=json_extract(wanted.value,'$[1]')
            AND f.version_str=json_extract(wanted.value,'$[2]')
            AND f.package_type=json_extract(wanted.value,'$[3]')
        LEFT JOIN firmware_metadata m USING(public_token)""", (json.dumps(keys),))
    metadata = []
    for row in rows:
        body, fingerprint = row.pop('metadata_json'), row.pop('content_hash')
        if body is not None:
            metadata.append(dict(public_token=row['public_token'],metadata_json=body,content_hash=fingerprint))
    insert_rows(shadow, 'stock_firmwares', rows)
    insert_rows(shadow, 'firmware_metadata', metadata)


def replay(shadow, value):
    conn = shadow._get_sqlite_conn()
    before = conn.total_changes
    conn.execute('SAVEPOINT receipt_probe')
    try:
        result = publish_release(shadow, value['device'], value['release'])
        if result['status'] == 'preserved_existing' and not result['metadata_changed'] and conn.total_changes == before:
            return 'confirmed_local'
        return 'publication_needed'
    except FirmwareIdentityPending:
        return 'identity_pending'
    finally:
        conn.execute('ROLLBACK TO receipt_probe')
        conn.execute('RELEASE receipt_probe')


def rebuild(remote, local, ledger, limit, after=''):
    work = islice(((key,value,fingerprint)
                    for stage,key,value,fingerprint in candidates(local,ledger,'xiaomi',respect_review=False)
                    if stage == 'release' and key > after and value['status'] == 'resolved' and not value['release'].get('refresh_source')),
                   limit)
    counts = Counter()
    with_shadow = ShadowDB(local_sqlite_path=':memory:')
    try:
        schema = (Path(__file__).parents[1] / 'schema.sql').read_text()
        # Read both old and model-keyed D1 datasets. Only the replay's exact
        # natural-key lookup can establish that a record already exists.
        schema = schema.replace('UNIQUE(device_codename, region_code, version_str, package_type)',
                                'UNIQUE(device_codename, model_code, region_code, version_str, package_type)')
        with_shadow.execute_script(schema)
        ensure_base_brands_and_anchor(with_shadow)
        from fmr_core.firmware_metadata_schema import ensure
        ensure(with_shadow, apply=True)
        while items := list(islice(work,BATCH_SIZE)):
            if remote.rows_read >= READ_LIMIT:
                counts['read_budget_stop'] += 1
                break
            candidate_snapshot(remote,with_shadow,items)
            statements = []
            for key, value, fingerprint in items:
                outcome = replay(with_shadow, value)
                counts[outcome] += 1
                if outcome == 'confirmed_local':
                    statements.extend(acknowledgement('xiaomi',key,fingerprint))
                elif outcome == 'identity_pending':
                    statements.append((XIAOMI_REVIEW_SQL,(key,fingerprint,int(time.time())+XIAOMI_REVIEW_DELAY)))
            # Advance by the last inspected key, even if every row needs
            # publication or review. Persist receipts and progress together.
            ledger.execute_batch([
                *statements,
                ('INSERT OR REPLACE INTO control(key,value) VALUES (?,?)', (CURSOR,items[-1][0])),
            ])
        return dict(counts)
    finally:
        with_shadow.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--limit',type=int,required=True)
    parser.add_argument('--resume',action='store_true')
    args = parser.parse_args()
    limit = args.limit
    if not 1 <= limit <= 70_000:
        parser.error('--limit must be between 1 and 70000')
    artifacts = Artifacts()
    resources = []
    path = ROOT/'publisher.sqlite'
    try:
        if not artifacts.restore('publisher', path, baseline=False) or not artifacts.restore('xiaomi', ROOT/'xiaomi.sqlite'):
            raise RuntimeError('Missing publication or Xiaomi checkpoint')
        ledger = open_ledger(path)
        resources.append(ledger)
        local = open_local(ROOT/'xiaomi.sqlite', initialize=False)
        resources.append(local)
        remote = ReadOnlyD1(enforce_limits=False)
        resources.append(remote)
        remote.metering = True
        after = ''
        if args.resume:
            progress = ledger.query('SELECT value FROM control WHERE key=?',(CURSOR,))
            if not progress:
                raise RuntimeError('No saved receipt reconciliation cursor')
            after = progress[0]['value']
        counts = rebuild(remote, local, ledger, limit, after)
        assert remote.rows_written == 0
        print(json.dumps({'event':'xiaomi_receipts_rebuilt','rows_read':remote.rows_read,
                          'rows_written':remote.rows_written,**counts}),flush=True)
    finally:
        for resource in reversed(resources):
            resource.close()
        artifacts.close()
    encrypt_file('publisher', path)


if __name__ == '__main__':
    main()
