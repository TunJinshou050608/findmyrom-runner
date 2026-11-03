from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

import pytest
from fmr_core.d1_client import D1Client
from fmr_core import identity_maintenance as migration
from fmr_core.schema_migrations import ensure_schema
from fmr_ingest_stock.catalogue_sync import publish_release
from fmr_ingest_stock.device_sync import ensure_base_brands_and_anchor
from fmr_core.crypto import generate_keypair

ROOT = Path(__file__).parents[1]


def day(n=4):
    return lambda: datetime(2026,11,n,1,0,tzinfo=timezone.utc)


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    private,_ = generate_keypair();monkeypatch.setenv('SIGN_PRIVATE_KEY_HEX',private)
    db = D1Client(local_sqlite_path=str(tmp_path/'migration.sqlite'))
    db.execute_script((ROOT/'schema.sql').read_text());ensure_base_brands_and_anchor(db)
    item={'vendor':'oneplus','upstream_id':'a','codename':'model-a','canonical_code':'oneplus-shared','name':'Sample'}
    for i in range(12):
        result=publish_release(db,item,{'version':f'BUILD.{i:03d}','region':'EU','kind':'full','download_url':f'https://ota.oneplus.net/{i}.zip'})
        if i%2==0:
            fid=db.query('SELECT id FROM stock_firmwares WHERE public_token=?',(result['public_token'],))[0]['id']
            db.execute("""INSERT INTO extracted_boot_images(public_token,firmware_id,device_codename,version_str,partition_name,variant_type,file_name,file_size_bytes,sha256_hash,gh_private_repo,gh_release_tag,gh_asset_id,ed25519_signature)
              VALUES (?,?,'oneplus-shared',?,'boot','stock','boot.img.gz',123,'hash','owner/vault','release',1,'signature')""",(f'bt_maintenance{i}',fid,f'BUILD.{i:03d}'))
    db.execute("UPDATE stock_firmwares SET extract_status='processing',extract_retries=2 WHERE id=1")
    db.execute("UPDATE sqlite_sequence SET seq=1000 WHERE name IN ('stock_firmwares','extracted_boot_images')")
    ensure_schema(db,allow_migration=True,through=5)
    original={table:db.query('SELECT * FROM '+table+' ORDER BY id') for table in migration.TABLES}
    yield db,original,item
    db.close()


def snapshot(db):
    return {table:db.query('SELECT * FROM '+table+' ORDER BY id') for table in migration.TABLES}


def test_complete_preserves_every_column_fk_sequence_and_keeps_sources_locked(seeded):
    db,original,item=seeded
    assert migration.migrate(db,now=day(),batch_size=3)=='complete'
    assert snapshot(db)==original
    assert not db.query('PRAGMA foreign_key_check')
    assert all(r['seq']==1000 for r in db.query("SELECT seq FROM sqlite_sequence WHERE name IN ('stock_firmwares','extracted_boot_images')"))
    assert db.query('SELECT official FROM catalogue_coverage')[0]['official']==12
    assert db.query('SELECT version FROM fmr_schema_migrations ORDER BY version')==[{'version':4},{'version':5},{'version':6}]
    assert all(db.query('SELECT * FROM '+table+'_before_v6 ORDER BY id')==original[table] for table in migration.TABLES)
    # Explicit release is separate from completion; remaining workflows stay paused.
    with pytest.raises(sqlite3.IntegrityError,match='awaiting maintenance release'):
        db.execute("UPDATE stock_firmwares SET extract_retries=0")
    for row in db.query("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'fmr_maintenance_%'"):
        db.execute('DROP TRIGGER '+migration.quote(row['name']))
    other=publish_release(db,{**item,'codename':'model-b'},{'version':'BUILD.000','region':'EU','kind':'full','download_url':'https://ota.oneplus.net/b.zip'})
    assert other['public_token']!=original['stock_firmwares'][0]['public_token']
    assert db.query('SELECT MAX(id) AS id FROM stock_firmwares')[0]['id']==1001
    assert db.query('SELECT official FROM catalogue_coverage')[0]['official']==13
    assert not db.query('PRAGMA foreign_key_check')


@pytest.mark.parametrize('lost_response',[False,True])
def test_failed_copy_stops_and_next_day_resumes_committed_cursor(seeded,monkeypatch,lost_response):
    db,original,_=seeded;native=db.execute_batch;count=0;stopped=False
    def fail(statements):
        nonlocal count,stopped
        assert not stopped,'No further database request after failure'
        if any(sql.startswith('INSERT INTO stock_firmwares_next(') for sql,_ in statements):
            count+=1
            if count==2:
                if lost_response:native(statements)
                stopped=True
                raise RuntimeError('quota or response lost')
        return native(statements)
    monkeypatch.setattr(db,'execute_batch',fail)
    with pytest.raises(RuntimeError,match='quota or response lost'):
        migration.migrate(db,now=day(),batch_size=3)
    monkeypatch.setattr(db,'execute_batch',native)
    state=migration.read_state(db)
    assert state['cursor']==(6 if lost_response else 3)
    assert snapshot(db)==original
    assert migration.migrate(db,now=day(),batch_size=3)=='already-attempted-today'
    assert migration.migrate(db,now=day(5),batch_size=3)=='complete'
    assert snapshot(db)==original


def test_failed_atomic_copy_rolls_back_cursor_and_data(seeded,monkeypatch):
    db,original,_=seeded;native=db.execute_batch
    def fail(statements):
        if any(sql.startswith('INSERT INTO stock_firmwares_next(') for sql,_ in statements):
            return native([*statements,('INSERT INTO no_such_table VALUES (1)',())])
        return native(statements)
    monkeypatch.setattr(db,'execute_batch',fail)
    with pytest.raises(sqlite3.OperationalError):migration.migrate(db,now=day(),batch_size=3)
    monkeypatch.setattr(db,'execute_batch',native)
    assert migration.read_state(db)['cursor']==0
    assert db.query('SELECT COUNT(*) AS n FROM stock_firmwares_next')[0]['n']==0
    assert snapshot(db)==original
    assert migration.migrate(db,now=day(5),batch_size=3)=='complete'


def test_unexpected_column_is_never_lost(seeded):
    db,original,_=seeded
    db.execute('ALTER TABLE stock_firmwares ADD COLUMN extra TEXT')
    with pytest.raises(RuntimeError,match='Unexpected source columns'):migration.migrate(db,now=day())
    assert db.query('SELECT COUNT(*) AS n FROM stock_firmwares')[0]['n']==12
    assert not migration.read_state(db)


def test_corrupted_shadow_prevents_cutover_and_leaves_original_untouched(seeded,monkeypatch):
    db,original,_=seeded;native=migration.verify_batch;corrupted=False
    def corrupt(db,state,size):
        nonlocal corrupted
        if not corrupted:
            db.execute("UPDATE stock_firmwares_next SET ed25519_signature='bad' WHERE id=1");corrupted=True
        return native(db,state,size)
    monkeypatch.setattr(migration,'verify_batch',corrupt)
    with pytest.raises(sqlite3.IntegrityError):migration.migrate(db,now=day(),batch_size=3)
    assert migration.read_state(db)['phase']=='verify_fw'
    assert snapshot(db)==original
    assert not db.query('SELECT version FROM fmr_schema_migrations WHERE version=6')


def test_prepare_only_does_not_copy_or_backfill_and_locks_every_application_writer(seeded):
    db,original,_=seeded;migration.prepare(db)
    assert migration.read_state(db) is None
    assert snapshot(db)==original
    for table in migration.TABLES+('devices','custom_resources','brand_universal_tools'):
        with pytest.raises(sqlite3.IntegrityError,match='maintenance'):
            db.execute('DELETE FROM '+table) if db.query('SELECT 1 FROM '+table+' LIMIT 1') else db.execute('INSERT INTO '+table+' DEFAULT VALUES')


def test_atomic_v6_entrypoint_cannot_destroy_a_batched_migration(seeded,monkeypatch):
    db,_,_=seeded
    migration.prepare(db);migration.initialize(db)
    with pytest.raises(RuntimeError,match='Batched identity maintenance'):
        ensure_schema(db,allow_migration=True,through=6)


def test_scheduling_is_limited_to_three_authorized_mornings_and_no_early_d1_calls():
    class NoDatabase:
        def __getattr__(self,name):raise AssertionError('No D1 access outside window')
    for date in [datetime(2026,11,3,1,tzinfo=timezone.utc),datetime(2026,11,4,0,59,tzinfo=timezone.utc),datetime(2026,11,7,1,tzinfo=timezone.utc),datetime(2027,11,4,1,tzinfo=timezone.utc)]:
        assert migration.migrate(NoDatabase(),now=lambda:date)=='outside-window'


def test_utc_rollover_stops_without_using_next_days_quota(seeded):
    db,_,_=seeded;calls=0
    def clock():
        nonlocal calls
        calls+=1
        return datetime(2026,11,4 if calls<3 else 5,1,tzinfo=timezone.utc)
    with pytest.raises(migration.StopForToday):migration.migrate(db,now=clock,batch_size=3)
    assert migration.read_state(db)['cursor']==3
    assert not db.query("SELECT utc_day FROM fmr_identity_days WHERE utc_day='2026-11-05'")


def test_review_hold_disables_migration_and_keeps_original_pause_settings():
    import yaml
    w=yaml.safe_load((ROOT/'.github/workflows/d1_identity_migration.yml').read_text())
    assert 'schedule' not in w['on']
    assert all(job['if'].startswith('false &&') for job in w['jobs'].values())
    assert w['concurrency']=={'group':'d1-schema-maintenance','cancel-in-progress':False}
    assert w['jobs']['migrate']['runs-on']=='ubuntu-latest'
    assert not any(s.get('continue-on-error') for j in w['jobs'].values() for s in j['steps'])
    originals=json.loads((ROOT/'ops/d1_maintenance_restore_workflows.json').read_text())
    assert len(originals)==17
    for path,text in originals.items():
        paused=yaml.safe_load((ROOT/path).read_text())
        assert all(j['if'].startswith('false &&') for j in paused['jobs'].values())
        original=yaml.safe_load(text)
        assert paused['jobs'].keys()==original['jobs'].keys()
        for name,job in paused['jobs'].items():
            assert {k:v for k,v in job.items() if k!='if'}=={k:v for k,v in original['jobs'][name].items() if k!='if'}


def test_native_d1_fixture_matches_the_python_transaction_plan():
    from export_identity_batches import export
    expected=json.loads((ROOT/'tests/fixtures/d1_identity_batches.json').read_text())
    assert export()==expected


def test_empty_tables_retain_deleted_record_sequence_high_watermarks(seeded):
    db,_,_=seeded
    db.execute('DELETE FROM extracted_boot_images');db.execute('DELETE FROM stock_firmwares')
    assert migration.migrate(db,now=day())=='complete'
    assert all(r['seq']==1000 for r in db.query("SELECT seq FROM sqlite_sequence WHERE name IN ('stock_firmwares','extracted_boot_images')"))
    assert not db.query('PRAGMA foreign_key_check')
