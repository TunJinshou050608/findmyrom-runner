"""Versioned, atomic D1 upgrades shared by every production publisher.

Never infer readiness from a local collector snapshot or modify the frozen baseline.
"""
import argparse
import hashlib
import json
from pathlib import Path

ROOT=Path(__file__).with_name('migrations')
CURRENT=6


def ensure_schema(db, *, allow_migration=False, through=5):
    if getattr(db,'_fmr_schema_ready',0)>=through:return
    rows=db.query("SELECT name FROM sqlite_master WHERE type='table' AND name='fmr_schema_migrations'")
    applied={r['version']:r['sha256'] for r in db.query('SELECT version,sha256 FROM fmr_schema_migrations')} if rows else {}
    for path in sorted(ROOT.glob('*.sql')):
        version=int(path.name.split('_',1)[0])
        if version>through:continue
        body=path.read_text();digest=hashlib.sha256(body.encode()).hexdigest()
        if version in applied:
            if applied[version]!=digest:raise RuntimeError('Applied D1 migration checksum mismatch: '+path.name)
            continue
        if not allow_migration:raise RuntimeError('D1 schema migration required before publication: '+path.name)
        # Existing installs of 0004 may predate this registry. Verify the complete
        # migration objects before adopting it; never run its backfill twice.
        if version==4 and db.query("SELECT name FROM sqlite_master WHERE type='table' AND name='catalogue_coverage'"):
            import re
            from .d1_client import split_sql_statements
            normalize=lambda sql:re.sub(r'\s+','',sql).rstrip(';').lower()
            expected={re.search(r'CREATE TRIGGER (\w+)',sql).group(1):normalize(sql) for sql in split_sql_statements(body) if sql.startswith('CREATE TRIGGER ')}
            actual={r['name']:normalize(r['sql']) for r in db.query("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND name LIKE 'coverage_%'")}
            if actual!=expected or len(db.query('SELECT id FROM catalogue_coverage WHERE id=1'))!=1:
                raise RuntimeError('Incomplete or altered coverage migration; refuse automatic repair')
            body=''
        if version==5:
            for table,column,kind in [('custom_resources','metadata_json',"TEXT NOT NULL DEFAULT '{}'"),('custom_resources','audit_batch_id','TEXT'),('brand_universal_tools','ed25519_signature',"TEXT NOT NULL DEFAULT ''")]:
                fields={r['name'] for r in db.query('PRAGMA table_info('+table+')')}
                if column not in fields:body='ALTER TABLE '+table+' ADD COLUMN '+column+' '+kind+';\n'+body
        if version==6:
            # Rebuilding a large indexed table can exceed a Free daily write cap.
            # Never start that transaction blindly; leave all source queues intact.
            import os
            count=db.query('SELECT COUNT(*) AS n FROM stock_firmwares')[0]['n']
            images=db.query('SELECT COUNT(*) AS n FROM extracted_boot_images')[0]['n']
            if db.mode!='sqlite' and count*7+images*6>60000 and os.environ.get('FMR_D1_LARGE_MIGRATION_CONFIRMED')!='true':
                raise RuntimeError('D1 identity migration requires a capacity-reviewed maintenance window; source queues retained')
            expected={'id','public_token','device_codename','region_code','model_code','version_str','android_version','package_type','file_size_str','md5_or_sha256','original_official_url','fast_cdn_url','published_date','ed25519_signature','extract_status','extract_retries','extract_error','updated_at'}
            if {r['name'] for r in db.query('PRAGMA table_info(stock_firmwares)')}!=expected:
                raise RuntimeError('Unexpected firmware columns; refuse lossy table rebuild')
            if {r['name'] for r in db.query('PRAGMA table_info(extracted_boot_images)')}!={'public_token', 'id', 'gh_asset_id', 'gh_private_repo', 'file_size_bytes', 'sha256_hash', 'working_votes', 'version_str', 'patch_tool_ver', 'partition_name', 'file_name', 'ed25519_signature', 'download_count', 'firmware_id', 'broken_votes', 'device_codename', 'gh_release_tag', 'created_at', 'variant_type'}:
                raise RuntimeError('Unexpected image columns; refuse lossy table rebuild')
            expected_indices={'stock_firmwares':{'idx_fw_device_ver','idx_fw_queue','idx_fw_version_lookup','idx_fw_pending_order'},'extracted_boot_images':{'idx_boot_search'}}
            for table,names in expected_indices.items():
                actual={r['name'] for r in db.query('PRAGMA index_list('+table+')') if not r['name'].startswith('sqlite_autoindex_')}
                if actual!=names:raise RuntimeError('Unexpected source indices; refuse lossy table rebuild')
            triggers={r['name'] for r in db.query("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name IN ('stock_firmwares','extracted_boot_images')")}
            if triggers!={'coverage_fw_insert','coverage_fw_delete','coverage_fw_update'}:
                raise RuntimeError('Unexpected source triggers; refuse lossy table rebuild')
            for table in db.query("SELECT name FROM sqlite_master WHERE type='table'"):
                name=table['name'];quoted='"'+name.replace('"','""')+'"'
                for fk in db.query('PRAGMA foreign_key_list('+quoted+')'):
                    if fk['table'] in ('stock_firmwares','extracted_boot_images') and name!='extracted_boot_images':
                        raise RuntimeError('Unexpected foreign-key dependant; refuse lossy table rebuild')
        registry="CREATE TABLE IF NOT EXISTS fmr_schema_migrations(version INTEGER PRIMARY KEY,sha256 TEXT NOT NULL,applied_at INTEGER NOT NULL DEFAULT (strftime('%s','now')));"
        # All DDL, backfill and the receipt must share one HTTP transaction.
        db.execute_script(registry+body+f"\nINSERT INTO fmr_schema_migrations(version,sha256) VALUES ({version},'{digest}');",max_batch_size=10000)
    db._fmr_schema_ready=through


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--apply',action='store_true');parser.add_argument('--through',type=int,choices=[5,6],default=5);args=parser.parse_args()
    from .d1_client import D1Client
    db=D1Client()
    try:
        ensure_schema(db,allow_migration=args.apply,through=args.through)
        print(json.dumps({'schema_version':args.through,'status':'ready'}))
    finally:db.close()

if __name__=='__main__':main()
