"""Offline, restartable v6 copy. Every data batch commits with its D1 cursor.

No firmware payload leaves D1. A failed/ambiguous request is never retried in
this process. The next permitted day reads the authoritative D1 checkpoint.
Source tables stay locked until an explicit maintenance release.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

from .d1_client import D1Client, split_sql_statements
from .schema_migrations import ROOT, ensure_schema

DAYS = {'2026-11-04', '2026-11-05', '2026-11-06'}
STATE = 'fmr_identity_maintenance'
GUARD = '_fmr_identity_assert'
TABLES = ('stock_firmwares', 'extracted_boot_images')
LOCK_TABLES = ('brands', 'devices', 'stock_firmwares', 'extracted_boot_images',
               'custom_resources', 'brand_universal_tools', 'maintainer_opt_outs',
               'sony_bundle_jobs')
BATCH_SIZE = 100
PLAN_VERSION = 'offline-v6-batched-1'


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def utc_now():
    return datetime.now(timezone.utc)


class StopForToday(RuntimeError):
    pass


class MaintenanceDB(D1Client):
    """No automatic retries, including reads and nominally idempotent writes."""
    def _request_cf_with_retry(self, payload, is_safe_to_retry=False, **kwargs):
        try:
            return super()._request_cf_with_retry(payload, is_safe_to_retry=False, max_retries=1)
        except RuntimeError as error:
            cause = error
            while cause is not None:
                response = getattr(cause, 'response', None)
                if response is not None:
                    try:
                        details = response.json().get('errors', [])
                    except (ValueError, AttributeError):
                        details = []
                    if details:
                        # Report API codes/messages, never headers, SQL parameters or tokens.
                        raise RuntimeError('D1 HTTP ' + str(response.status_code) + ': ' + json.dumps(details)) from error
                cause = cause.__cause__
            raise


def plan():
    body = (ROOT / '0006_firmware_model_identity.sql').read_text()
    parts = split_sql_statements(body)
    creates = [s for s in parts if s.startswith('CREATE TABLE ') and
               any(s.startswith('CREATE TABLE ' + t + '_next ') for t in TABLES)]
    indices = [s for s in parts if s.startswith(('CREATE INDEX ', 'CREATE UNIQUE INDEX '))]
    triggers = [s for s in parts if s.startswith('CREATE TRIGGER ')]
    assert len(creates) == 2 and len(indices) == 5 and len(triggers) == 12
    # Empty shadow tables get their final indexes before any copy. This avoids
    # a non-resumable full-table CREATE INDEX on the last day.
    indices = [s.replace(' ON stock_firmwares(', ' ON stock_firmwares_next(')
                .replace(' ON extracted_boot_images(', ' ON extracted_boot_images_next(') for s in indices]
    return creates, indices, triggers, hashlib.sha256(body.encode()).hexdigest()


def source_shape(db):
    """Reject drift before dropping/rebuilding anything; retain complete rows."""
    expected = {
        'stock_firmwares': {'id','public_token','device_codename','region_code','model_code','version_str','android_version','package_type','file_size_str','md5_or_sha256','original_official_url','fast_cdn_url','published_date','ed25519_signature','extract_status','extract_retries','extract_error','updated_at'},
        'extracted_boot_images': {'public_token','id','gh_asset_id','gh_private_repo','file_size_bytes','sha256_hash','working_votes','version_str','patch_tool_ver','partition_name','file_name','ed25519_signature','download_count','firmware_id','broken_votes','device_codename','gh_release_tag','created_at','variant_type'},
    }
    indices = {'stock_firmwares': {'idx_fw_device_ver','idx_fw_queue','idx_fw_pending_order'},
               'extracted_boot_images': {'idx_boot_search'}}
    columns = {}
    for table in TABLES:
        fields = db.query('PRAGMA table_info(' + table + ')')
        if {r['name'] for r in fields} != expected[table]:
            raise RuntimeError('Unexpected source columns: ' + table)
        columns[table] = [r['name'] for r in fields]
        actual = {r['name'] for r in db.query('PRAGMA index_list(' + table + ')')
                  if not r['name'].startswith('sqlite_autoindex_')}
        if actual != indices[table]:
            raise RuntimeError('Unexpected source indexes: ' + table)
    actual = {r['name'] for r in db.query("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name IN ('stock_firmwares','extracted_boot_images')")
              if not r['name'].startswith('fmr_maintenance_lock_')}
    if actual != {'coverage_fw_insert','coverage_fw_delete','coverage_fw_update','search_fw_insert','search_fw_delete','search_fw_update'}:
        raise RuntimeError('Unexpected source triggers')
    for row in db.query("SELECT name FROM sqlite_master WHERE type='table'"):
        name = row['name']
        for fk in db.query('PRAGMA foreign_key_list(' + quote(name) + ')'):
            if fk['table'] in TABLES and name != 'extracted_boot_images':
                raise RuntimeError('Unexpected foreign-key dependant: ' + name)
    return columns


def lock_statements(db):
    present = {r['name'] for r in db.query("SELECT name FROM sqlite_master WHERE type='table'")}
    # Lock every application table, including legacy names, not merely known
    # writers. New maintenance/search/checkpoint tables are excluded explicitly.
    app_tables = present - {'sqlite_sequence','d1_migrations','fmr_schema_migrations',
        'catalogue_coverage','firmware_search','fmr_catalogue_budget',STATE,
        'fmr_identity_days',GUARD}
    app_tables = {t for t in app_tables if not t.startswith(('sqlite_', '_cf_', 'stock_firmwares_next', 'extracted_boot_images_next'))}
    statements = []
    for table in sorted(app_tables):
        for action in ('INSERT','UPDATE','DELETE'):
            name = 'fmr_maintenance_lock_' + table + '_' + action.lower()
            statements.append((f'CREATE TRIGGER IF NOT EXISTS {quote(name)} BEFORE {action} ON {quote(table)} BEGIN SELECT RAISE(ABORT,\'D1 migration maintenance; publisher checkpoint must remain pending\'); END', ()))
    return statements


def prepare(db):
    """Safe to run on setup day: metadata only, no data migration or backfill."""
    db.execute_batch([
        ('''CREATE TABLE IF NOT EXISTS fmr_identity_days(
          utc_day TEXT PRIMARY KEY,started_at TEXT NOT NULL)''', ()),
        ('''CREATE TABLE IF NOT EXISTS _fmr_identity_assert(value INTEGER NOT NULL CHECK(value=1))''', ()),
        *lock_statements(db),
    ])


def assertion(condition, params=()):
    return [(f'INSERT INTO {GUARD}(value) SELECT CASE WHEN ({condition}) THEN 1 ELSE 0 END', params),
            (f'DELETE FROM {GUARD}', ())]


def read_state(db):
    exists = db.query("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (STATE,))
    return db.query('SELECT * FROM ' + STATE + ' WHERE id=1')[0] if exists else None


def initialize(db):
    columns = source_shape(db)
    creates, indices, _, digest = plan()
    tables = {r['name'] for r in db.query("SELECT name FROM sqlite_master WHERE type='table'")}
    if any(t + '_next' in tables or t + '_before_v6' in tables for t in TABLES):
        raise RuntimeError('Untracked shadow/backup tables; refuse overwrite')
    sql = [(f'''CREATE TABLE {STATE}(id INTEGER PRIMARY KEY CHECK(id=1),
       plan TEXT NOT NULL,sha256 TEXT NOT NULL,phase TEXT NOT NULL,cursor INTEGER NOT NULL,
       columns_json TEXT NOT NULL,fw_total INTEGER NOT NULL,image_total INTEGER NOT NULL,
       verified_fw INTEGER NOT NULL DEFAULT 0,verified_images INTEGER NOT NULL DEFAULT 0)''', ())]
    sql += [(s, ()) for s in creates]
    # All application access is paused. Original secondary indexes can be
    # removed here; old UNIQUE constraints and all original data are retained.
    for name in ('idx_fw_device_ver','idx_fw_queue','idx_fw_pending_order','idx_boot_search'):
        sql.append(('DROP INDEX ' + name, ()))
    sql += [(s, ()) for s in indices]
    sql.append((f'''INSERT INTO {STATE}(id,plan,sha256,phase,cursor,columns_json,fw_total,image_total)
       VALUES (1,?,?,'copy_fw',0,?,(SELECT COUNT(*) FROM stock_firmwares),
       (SELECT COUNT(*) FROM extracted_boot_images))''', (PLAN_VERSION, digest, json.dumps(columns))))
    db.execute_batch(sql)
    return read_state(db)


def copy_batch(db, state, batch_size):
    table = TABLES[0] if state['phase'] == 'copy_fw' else TABLES[1]
    rows = db.query('SELECT id FROM ' + table + ' WHERE id>? ORDER BY id LIMIT ?', (state['cursor'], batch_size))
    if not rows:
        phase = 'copy_images' if table == TABLES[0] else 'verify_fw'
        db.execute_batch(assertion(f"EXISTS(SELECT 1 FROM {STATE} WHERE phase=? AND cursor=?)", (state['phase'], state['cursor'])) +
                         [(f'UPDATE {STATE} SET phase=?,cursor=0 WHERE id=1', (phase,))])
        return
    end = rows[-1]['id']
    cols = json.loads(state['columns_json'])[table]
    names = ','.join(map(quote, cols))
    same = ' AND '.join('s.' + quote(c) + ' IS n.' + quote(c) for c in cols)
    sql = assertion(f"EXISTS(SELECT 1 FROM {STATE} WHERE phase=? AND cursor=?)", (state['phase'], state['cursor']))
    sql += [(f'INSERT INTO {table}_next({names}) SELECT {names} FROM {table} WHERE id>? AND id<=?', (state['cursor'], end))]
    sql += assertion(f'NOT EXISTS(SELECT 1 FROM {table} s LEFT JOIN {table}_next n ON n.id=s.id WHERE s.id>? AND s.id<=? AND NOT ({same}))', (state['cursor'], end))
    sql += [(f'UPDATE {STATE} SET cursor=? WHERE id=1', (end,))]
    db.execute_batch(sql)


def verify_batch(db, state, batch_size):
    table = TABLES[0] if state['phase'] == 'verify_fw' else TABLES[1]
    rows = db.query('SELECT id FROM ' + table + '_next WHERE id>? ORDER BY id LIMIT ?', (state['cursor'], batch_size))
    if not rows:
        phase = 'verify_images' if table == TABLES[0] else 'cutover'
        db.execute_batch([(f'UPDATE {STATE} SET phase=?,cursor=0 WHERE id=1', (phase,))])
        return
    end = rows[-1]['id']; cols = json.loads(state['columns_json'])[table]
    same = ' AND '.join('s.' + quote(c) + ' IS n.' + quote(c) for c in cols)
    counter = 'verified_fw' if table == TABLES[0] else 'verified_images'
    sql = assertion(f'NOT EXISTS(SELECT 1 FROM {table}_next n LEFT JOIN {table} s ON n.id=s.id WHERE n.id>? AND n.id<=? AND NOT ({same}))', (state['cursor'], end))
    if table == TABLES[1]:
        sql += assertion('NOT EXISTS(SELECT 1 FROM extracted_boot_images_next i LEFT JOIN stock_firmwares_next f ON f.id=i.firmware_id WHERE i.id>? AND i.id<=? AND f.id IS NULL)', (state['cursor'], end))
    sql += [(f'UPDATE {STATE} SET cursor=?,{counter}={counter}+? WHERE id=1', (end, len(rows)))]
    db.execute_batch(sql)


def cutover(db, state):
    _, _, triggers, digest = plan()
    sql = assertion(f'EXISTS(SELECT 1 FROM {STATE} WHERE phase=\'cutover\' AND verified_fw=fw_total AND verified_images=image_total)')
    # Preserve high-water marks, including deleted records with higher IDs.
    for table in TABLES:
        sql += [('UPDATE sqlite_sequence SET seq=MAX(seq,COALESCE((SELECT seq FROM sqlite_sequence WHERE name=?),0)) WHERE name=?', (table, table + '_next')),
                ('INSERT INTO sqlite_sequence(name,seq) SELECT ?,seq FROM sqlite_sequence WHERE name=? AND NOT EXISTS(SELECT 1 FROM sqlite_sequence WHERE name=?)', (table+'_next',table,table+'_next'))]
    for trigger in triggers:
        name = trigger.split()[2]
        sql.append(('DROP TRIGGER ' + name, ()))
    # Renaming keeps large DROP/DELETE costs out of the cutover transaction.
    # Backups remain locked, so rollback evidence survives the three-day window.
    for table in TABLES:
        sql.append((f'ALTER TABLE {table} RENAME TO {table}_before_v6', ()))
    for table in TABLES:
        sql.append((f'ALTER TABLE {table}_next RENAME TO {table}', ()))
    sql += [(s, ()) for s in triggers]
    sql += [("INSERT INTO fmr_schema_migrations(version,sha256) VALUES (6,?)", (digest,)),
            (f"UPDATE {STATE} SET phase='complete',cursor=0 WHERE id=1", ())]
    # Keep newly renamed live tables frozen too until maintenance is released.
    for table in TABLES:
        for action in ('INSERT','UPDATE','DELETE'):
            name = 'fmr_maintenance_live_' + table + '_' + action.lower()
            sql.append((f'CREATE TRIGGER {name} BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT,\'D1 migration complete; awaiting maintenance release\'); END', ()))
    db.execute_batch(sql)


def check_state(state):
    if state['plan'] != PLAN_VERSION or state['sha256'] != plan()[3]:
        raise RuntimeError('Migration plan/checksum changed; refuse to mix checkpoints')
    if state['phase'] not in ('copy_fw','copy_images','verify_fw','verify_images','cutover','complete'):
        raise RuntimeError('Unknown migration phase')


def record(state, log_path):
    data = {k: state[k] for k in ('phase','cursor','fw_total','image_total','verified_fw','verified_images')}
    print(json.dumps(data), flush=True)
    if log_path:
        Path(log_path).write_text(json.dumps(data, indent=2) + '\n')


def migrate(db, *, now=utc_now, batch_size=BATCH_SIZE, log_path=None):
    started = now();day = started.strftime('%Y-%m-%d')
    if day not in DAYS or started.hour < 1:
        return 'outside-window'
    prepare(db)
    if db.query('SELECT utc_day FROM fmr_identity_days WHERE utc_day=?', (day,)):
        return 'already-attempted-today'
    db.execute('INSERT INTO fmr_identity_days(utc_day,started_at) VALUES (?,?)', (day, started.isoformat()), is_idempotent=False)
    # Additive v5 is necessary for both publishers and the final v6 triggers.
    # A failure here stops today's run before a single firmware is copied.
    ensure_schema(db, allow_migration=True, through=5)
    state = read_state(db)
    if state is None:
        if db.query('SELECT version FROM fmr_schema_migrations WHERE version=6'):
            return 'already-upgraded'
        state = initialize(db)
    while True:
        if now().strftime('%Y-%m-%d') != day:
            raise StopForToday('UTC day changed; do not borrow tomorrow\'s quota')
        check_state(state);record(state, log_path)
        if state['phase'] == 'complete':
            return 'complete'
        if state['phase'].startswith('copy_'):
            copy_batch(db, state, batch_size)
        elif state['phase'].startswith('verify_'):
            verify_batch(db, state, batch_size)
        else:
            cutover(db, state)
        state = read_state(db)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--prepare-only', action='store_true')
    parser.add_argument('--log-path', default='migration-progress.json')
    args = parser.parse_args()
    # Scheduled workflows may exist in later years, but never touch D1 again.
    allowed = (DAYS | {'2026-11-03'}) if args.prepare_only else DAYS
    if utc_now().strftime('%Y-%m-%d') not in allowed or (not args.prepare_only and utc_now().hour < 1):
        print(json.dumps({'status':'outside-authorized-window'}));return 0
    db = MaintenanceDB()
    try:
        if args.prepare_only:
            prepare(db);status = 'sources-locked'
        else:
            status = migrate(db, log_path=args.log_path)
        print(json.dumps({'status':status}), flush=True)
        return 0
    except Exception as error:
        # Do not query/write D1 after ANY error, including a lost response after
        # a successful commit. A local log may lag; D1 is authoritative tomorrow.
        print(json.dumps({'status':'stopped','error':type(error).__name__,
                          'message':str(error)[:1500],
                          'next':'Next authorized day resumes the D1 checkpoint; no retry today.'}), flush=True)
        return 1
    finally:
        db.close()


if __name__ == '__main__':
    sys.exit(main())
