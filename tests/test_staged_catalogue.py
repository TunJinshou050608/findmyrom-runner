import sqlite3
"""Real SQLite + crypto with fake source/network: cross-job durability and D1 isolation."""
import io
import json
from pathlib import Path
import shutil
import zipfile
import httpx
import pytest
from cryptography.exceptions import InvalidTag
from fmr_core.d1_client import D1Client
from fmr_ingest_stock import staged_catalogue as staged,catalogue_publish as publisher
from fmr_ingest_stock.catalogue_store import Store
from fmr_ingest_stock.d1_budget import BudgetDB
from test_vendor_catalogue import env,Source,clients


def test_snapshot_roundtrip_tamper_and_scope_isolation(env):
    payload=b'sensitive source metadata'*100
    blob=staged.seal(payload,'google')
    assert payload not in blob and staged.unseal(blob,'google')==payload
    with pytest.raises(InvalidTag):staged.unseal(blob,'xiaomi')
    with pytest.raises(InvalidTag):staged.unseal(blob[:-1]+bytes([blob[-1]^1]),'google')


def test_snapshot_writer_and_reader_enforce_separate_transfer_and_decoded_limits(env,monkeypatch):
    monkeypatch.setattr(staged,'MAX_SNAPSHOT',1000)
    monkeypatch.setattr(staged,'MAX_ENCRYPTED_SNAPSHOT',200)
    # A highly compressible database can exceed its transfer size safely.
    blob=staged.seal(b'x'*1000,'xiaomi')
    assert len(blob)<200 and staged.unseal(blob,'xiaomi')==b'x'*1000
    with pytest.raises(ValueError,match='snapshot size'):staged.seal(b'x'*1001,'xiaomi')
    monkeypatch.setattr(staged,'MAX_SNAPSHOT',999)
    with pytest.raises(ValueError,match='snapshot size'):staged.unseal(blob,'xiaomi')
    monkeypatch.setattr(staged,'MAX_ENCRYPTED_SNAPSHOT',20)
    with pytest.raises(ValueError,match='Oversize'):staged.unseal(blob,'xiaomi')


def test_artifact_restore_redirect_keeps_token_off_blob_and_authenticates_scope(env,tmp_path,monkeypatch):
    archive=io.BytesIO()
    with zipfile.ZipFile(archive,'w') as z:z.writestr('google.sqlite.enc',staged.seal(b'SQLite bytes','google'))
    calls=[]
    def handler(req):
        calls.append(req)
        if req.url.host=='api.github.com':
            assert req.headers.get('Authorization')=='Bearer ephemeral'
            if req.url.path.endswith('/artifacts'):
                assert req.url.params['name']=='catalogue-v3-google'
                return httpx.Response(200,json={'artifacts':[{'id':9,'expired':False}]})
            return httpx.Response(302,headers={'Location':'https://storage.example/signed'})
        assert not req.headers.get('Authorization')
        return httpx.Response(200,content=archive.getvalue())
    real=httpx.Client
    monkeypatch.setattr(staged.httpx,'Client',lambda *a,**kw:real(*a,**kw,transport=httpx.MockTransport(handler)))
    artifacts=staged.Artifacts()
    try:
        path=tmp_path/'restored.sqlite';assert artifacts.restore('google',path)==9
        assert path.read_bytes()==b'SQLite bytes'
    finally:artifacts.close()
    assert len(calls)==3


def test_collector_restarts_from_encrypted_state_and_never_constructs_cloud_d1(env,tmp_path,monkeypatch):
    _,_=env
    root=tmp_path/'state';root.mkdir();monkeypatch.setattr(staged,'ROOT',root)
    initial=tmp_path/'initial.sqlite';db=staged.open_local(initial)
    store=Store(db,'oneplus');store.put(store.key('migration'),{'done':True,'cursor':'','imported':0});db.close()
    snapshots=[staged.seal(initial.read_bytes(),'oneplus')]
    class Artifacts:
        def restore(self,scope,path,**kw):path.write_bytes(staged.unseal(snapshots[-1],scope));return 1
        def close(self):pass
    monkeypatch.setattr(staged,'Artifacts',Artifacts)
    source=Source(5);clients(monkeypatch,source)
    assert staged.collect('oneplus')==0
    snapshots.append((root/'upload/oneplus.sqlite.enc').read_bytes())
    requests=len(source.requests)
    assert staged.collect('oneplus')==0
    assert len(source.requests)==requests
    db=D1Client(local_sqlite_path=str(root/'oneplus.sqlite'))
    assert Store(db,'oneplus').counts()=={'models':{'ok':5},'releases':{'resolved':5}}
    assert not db.query('SELECT * FROM stock_firmwares');db.close()
    assert not source.dispatches # Handoff happens only after durable upload.


def test_publish_duplicate_restart_and_partial_failure_keep_existing_tokens(env,tmp_path):
    remote,_=env;ledger=publisher.open_ledger(tmp_path/'ledger.sqlite')
    device={'vendor':'oneplus','upstream_id':'sample','name':'Sample'}
    row={'version':'BUILD.1','region':'EU','kind':'Full OTA','download_url':'https://ota.oneplus.net/BUILD.1.zip'}
    item=('release','key',{'device':device,'release':row},'fingerprint')
    assert publisher.sync_one(remote,ledger,'oneplus',item)
    token=remote.query('SELECT public_token FROM stock_firmwares')[0]['public_token']
    # Lost acknowledgement: retry must preserve the successful D1 record/signature.
    ledger.execute('DELETE FROM published')
    assert publisher.sync_one(remote,ledger,'oneplus',item)
    assert remote.query('SELECT public_token FROM stock_firmwares')==[{'public_token':token}]
    local=staged.open_local(tmp_path/'source.sqlite');store=Store(local,'oneplus')
    store.put('v2:oneplus:release:key',dict(item[2],status='resolved',published=False))
    pending=list(publisher.candidates(local,ledger,'oneplus'));assert len(pending)==1
    assert publisher.sync_one(remote,ledger,'oneplus',pending[0])
    logs=[];remote._get_sqlite_conn().set_trace_callback(logs.append)
    assert list(publisher.candidates(local,ledger,'oneplus'))==[]
    assert logs==[] # Dedup needs zero D1 reads/writes.
    local.close();ledger.close()


def test_publish_failed_write_does_not_acknowledge(env,tmp_path,monkeypatch):
    db,_=env;ledger=publisher.open_ledger(tmp_path/'ledger.sqlite')
    monkeypatch.setattr(publisher,'publish_release',lambda *a:(_ for _ in ()).throw(RuntimeError('network')))
    with pytest.raises(RuntimeError):publisher.sync_one(db,ledger,'google',('release','key',{'device':{},'release':{}},'hash'))
    assert ledger.query('SELECT * FROM published')==[];ledger.close()


def test_publish_global_ten_minute_slot_no_change_avoids_d1(env,tmp_path,monkeypatch,capsys):
    root=tmp_path/'state';root.mkdir();monkeypatch.setattr(publisher,'ROOT',root);monkeypatch.setattr(staged,'ROOT',root)
    class Artifacts:
        def restore(self,*a,**kw):return False
        def close(self):pass
    monkeypatch.setattr(publisher,'Artifacts',Artifacts)
    monkeypatch.setattr(publisher,'BudgetDB',lambda **kw:pytest.fail('No changes must not connect to D1'))
    assert publisher.main()==0
    assert publisher.main()==0
    assert '跳过重复触发' in capsys.readouterr().out


def test_publication_pause_does_not_construct_clients_or_touch_confirmation_points(monkeypatch):
    from fmr_extractor import cli
    from fmr_ingest_stock.catalogue_publish_monitor import run
    monkeypatch.setenv('FMR_D1_WRITES_PAUSED','true')
    def forbidden(*a,**kw):pytest.fail('Paused operation attempted database/network work')
    monkeypatch.setattr(publisher,'Artifacts',forbidden)
    monkeypatch.setattr(publisher,'open_ledger',forbidden)
    monkeypatch.setattr(cli,'D1Client',forbidden)
    monkeypatch.setattr(cli,'VaultClient',forbidden)
    assert publisher.main()==0
    assert run(clock=forbidden,sleep=forbidden,publish=forbidden)==0
    assert cli.main(['--requeue-skipped'])==0


def test_budget_settlement_refunds_unused_reservation_and_keeps_overhead(tmp_path):
    db=BudgetDB(local_sqlite_path=str(tmp_path/'budget.sqlite'))
    assert db.acquire();db.rows_read=1000;db.rows_written=300;db.settle()
    row=db.query('SELECT * FROM fmr_catalogue_budget')[0]
    assert row['reads_reserved']==1100 and row['writes_reserved']==320
    db.close()


def test_workflow_single_writer_and_upload_before_handoff():
    import yaml
    root=Path(__file__).parents[1]/'.github/workflows'
    for path in root.glob('catalogue_*.yml'):
        data=yaml.safe_load(path.read_text())
        if path.name=='catalogue_bootstrap.yml':
            assert data['concurrency']['group']=='catalogue-bootstrap'
            assert data['concurrency']['cancel-in-progress'] is False
            steps=data['jobs']['restore']['steps']
            credential_steps=[step for step in steps if 'CF_API_TOKEN' in step.get('env',{})]
            assert len(credential_steps)==1 and 'staged_catalogue baseline' in credential_steps[0]['run']
            upload=next(i for i,step in enumerate(steps) if step.get('uses')=='actions/upload-artifact@v4')
            resume=next(i for i,step in enumerate(steps) if 'fmr_ingest_stock.quota_resume' in step.get('run',''))
            assert upload<resume
            continue
        if path.name=='catalogue_publish.yml':
            assert data['on']['schedule'][0]['cron']=='*/10 * * * *'
            assert data['jobs']['publish']['concurrency']['group']=='catalogue-d1-publisher'
        else:
            assert 'CF_API_TOKEN' not in path.read_text()
            steps=data['jobs']['catalogue']['steps']
            upload=next(i for i,s in enumerate(steps) if s.get('id')=='snapshot')
            handoff=next(i for i,s in enumerate(steps) if 'staged_catalogue next' in s.get('run',''))
            assert upload<handoff


def test_one_time_migration_reuses_legacy_and_v2_without_any_source_requests(env,tmp_path,monkeypatch):
    remote,_=env
    from fmr_ingest_stock.catalogue_job import save as legacy_save,task_key
    device={'vendor':'google','upstream_id':'pixel','name':'Pixel','codename':'pixel'}
    row={'version':'ABC.1','region':'Global','kind':'full_ota','download_url':'https://dl.google.com/dl/android/aosp/pixel-ota.zip'}
    legacy_save(remote,task_key('legacy','release','one'),{'device':device,'release':row,'done':True})
    v2=Store(remote,'google');v2.put(v2.key('model','pixel'),{'device':device,'status':'ok','next_check':9999999999})
    root=tmp_path/'state';root.mkdir();monkeypatch.setattr(staged,'ROOT',root)
    class MigrationDB:
        rows_read=0
        def acquire(self):return True
        def query(self,sql,params):
            result=remote.query(sql,params);self.rows_read+=len(result);return result
        def close(self):pass
    import fmr_ingest_stock.d1_budget as budgets
    monkeypatch.setattr(budgets,'BudgetDB',lambda **kw:MigrationDB())
    for name in ('CF_ACCOUNT_ID','CF_API_TOKEN','CF_D1_DATABASE_ID'):monkeypatch.setenv(name,'synthetic')
    staged.baseline()
    assert len(list((root/'upload').glob('*.sqlite.enc')))==len(staged.job.VENDORS)
    content=staged.unseal((root/'upload/google.sqlite.enc').read_bytes(),'google')
    restored=tmp_path/'migrated.sqlite';restored.write_bytes(content)
    db=D1Client(local_sqlite_path=str(restored));store=Store(db,'google')
    assert store.counts()=={'models':{'ok':1},'releases':{'resolved':1}}
    assert store.rows('release')[0][1]['release']['download_url']==row['download_url'];db.close()
    assert len(remote.query("SELECT * FROM crawler_sources WHERE source_kind='oem_catalogue'"))==1


def test_settlement_outage_still_saves_successful_publication_acknowledgements(env,tmp_path,monkeypatch):
    root=tmp_path/'state';root.mkdir()
    monkeypatch.setattr(publisher,'ROOT',root);monkeypatch.setattr(staged,'ROOT',root)
    closed=[]
    class Artifacts:
        def restore(self,scope,path,**kwargs):
            if scope=='publisher':return False
            path.touch();return True
        def close(self):closed.append('artifacts')
    class Local:
        def close(self):pass
    class DB:
        _fmr_schema_ready=5
        rows_read=1;rows_written=1
        def __init__(self,**kwargs):pass
        def acquire(self):return True
        def settle(self):raise RuntimeError('simulated settlement outage')
        def close(self):closed.append('database')
    def synced(db,ledger,vendor,item,outcomes=None):
        ledger.execute('INSERT INTO published(vendor,key,hash) VALUES (?,?,?)',('google','successful-remote-item','hash'))
        return True
    monkeypatch.setattr(publisher,'Artifacts',Artifacts);monkeypatch.setattr(publisher,'VENDORS',('google',))
    monkeypatch.setattr(publisher,'open_local',lambda *a,**kw:Local())
    monkeypatch.setattr(publisher,'candidates',lambda *_:(item for item in [('synthetic',)]))
    monkeypatch.setattr(publisher,'BudgetDB',DB);monkeypatch.setattr(publisher,'sync_one',synced)
    for name in ('CF_ACCOUNT_ID','CF_API_TOKEN','CF_D1_DATABASE_ID'):monkeypatch.setenv(name,'synthetic')
    with pytest.raises(RuntimeError,match='settlement outage'):publisher.main()
    restored=tmp_path/'restored.sqlite'
    restored.write_bytes(staged.unseal((root/'upload/publisher.sqlite.enc').read_bytes(),'publisher'))
    ledger=publisher.open_ledger(restored)
    try:assert ledger.query('SELECT key FROM published')==[{'key':'successful-remote-item'}]
    finally:ledger.close()
    assert closed==['database','artifacts']


def test_metadata_runner_routing_keeps_binary_and_pr_jobs_off_local_host():
    import yaml
    root=Path(__file__).parents[1]/'.github/workflows'
    for path in root.glob('catalogue_*.yml'):
        for job_name,job in yaml.safe_load(path.read_text())['jobs'].items():
            if job_name=='schema':
                assert job['concurrency']['group']=='d1-schema-maintenance'
                continue
            if job_name=='telegram':
                assert job['runs-on']==['self-hosted','findmyrom-metadata']
                continue
            assert 'FMR_CATALOGUE_RUNNER' in job['runs-on']
            if path.name in ('catalogue_huawei.yml','catalogue_honor.yml'):
                assert job['if']=="github.ref == 'refs/heads/main' && false"
            elif path.name=='catalogue_publish.yml' and job_name=='samsung':
                assert job['if']=="github.ref == 'refs/heads/main' && vars.FMR_D1_WRITES_PAUSED == 'true' && vars.FMR_SAMSUNG_D1_WRITES_PAUSED != 'true' && inputs.scope != 'tg'"
                assert job['env']['FMR_CATALOGUE_PUBLISH_VENDORS']=='samsung'
                assert job['concurrency']['group']=='catalogue-d1-publisher'
            elif path.name=='catalogue_publish.yml':
                assert job['if']=="github.ref == 'refs/heads/main' && vars.FMR_D1_WRITES_PAUSED != 'true' && github.event_name != 'push' && inputs.scope != 'tg'"
            else:assert job['if']=="github.ref == 'refs/heads/main'"
    for name in ('ci.yml','telegram_fetch.yml','telegram_clean.yml','services.yml'):
        assert 'FMR_CATALOGUE_RUNNER' not in (root/name).read_text()


def test_job_tokens_only_verify_workflow_state_and_dispatch(env,monkeypatch):
    from fmr_ingest_stock import quota_resume
    requests=[]
    def handler(req):
        requests.append(req)
        assert req.method not in ('PUT','DELETE')
        if req.url.path.endswith('/actions/runs'):return httpx.Response(200,json={'workflow_runs':[]})
        if req.method=='GET':return httpx.Response(200,json={'state':'disabled_manually'})
        return httpx.Response(204)
    real=httpx.Client
    monkeypatch.setattr(staged.httpx,'Client',lambda *a,**kw:real(*a,**kw,transport=httpx.MockTransport(handler)))
    staged.prepare()
    assert not any(str(r.url).endswith('/batch_extract.yml') for r in requests)
    def resume_handler(req):
        requests.append(req);assert req.method not in ('PUT','DELETE')
        return httpx.Response(200,json={'state':'active'}) if req.method=='GET' else httpx.Response(204)
    monkeypatch.setattr(staged.httpx,'Client',lambda *a,**kw:real(*a,**kw,transport=httpx.MockTransport(resume_handler)))
    assert quota_resume.main()==0
    dispatches=[str(req.url) for req in requests if req.method=='POST']
    assert dispatches[0].endswith('/catalogue_publish.yml/dispatches')
    assert len(dispatches)==len(quota_resume.VENDORS)+1


def test_shared_baseline_is_downloaded_once_for_multiple_vendors(env,tmp_path,monkeypatch):
    archive=io.BytesIO()
    with zipfile.ZipFile(archive,'w') as z:
        for vendor in ('google','xiaomi'):z.writestr(vendor+'.sqlite.enc',staged.seal(vendor.encode(),vendor))
    downloads=[]
    def handler(req):
        if req.url.path.endswith('/artifacts'):
            items=[{'id':9,'expired':False}] if req.url.params['name']==staged.BASELINE else []
            return httpx.Response(200,json={'artifacts':items})
        downloads.append(str(req.url));return httpx.Response(200,content=archive.getvalue())
    real=httpx.Client
    monkeypatch.setattr(staged.httpx,'Client',lambda *a,**kw:real(*a,**kw,transport=httpx.MockTransport(handler)))
    artifacts=staged.Artifacts()
    try:
        for vendor in ('google','xiaomi'):
            path=tmp_path/(vendor+'.sqlite');assert artifacts.restore(vendor,path)==9
            assert path.read_bytes()==vendor.encode()
    finally:artifacts.close()
    assert len(downloads)==1


def test_fixed_host_uses_persistent_state_without_remote_checkpoint_reads(env,tmp_path,monkeypatch):
    path=tmp_path/'vendor.sqlite';db=staged.open_local(path);db.close()
    staged.checkpoint_stamp(path,artifact_id=9,writer_run_id='test-run')
    monkeypatch.setenv('FMR_CATALOGUE_LOCAL_STATE','true')
    artifacts=staged.Artifacts()
    monkeypatch.setattr(artifacts,'list',lambda *_:[{'id':9,'expired':False}])
    monkeypatch.setattr(artifacts.client,'get',lambda *_a,**_kw:pytest.fail('Warm restart must not download a snapshot'))
    try:assert artifacts.restore('google',path)=='local'
    finally:artifacts.close()


def test_corrupt_persistent_state_stops_instead_of_restarting_history(env,tmp_path,monkeypatch):
    import sqlite3
    path=tmp_path/'broken.sqlite';path.write_bytes(b'broken state')
    monkeypatch.setenv('FMR_CATALOGUE_LOCAL_STATE','true');artifacts=staged.Artifacts()
    monkeypatch.setattr(artifacts,'list',lambda *_:pytest.fail('Do not hide corrupt local state'))
    try:
        with pytest.raises(sqlite3.DatabaseError):artifacts.restore('google',path)
    finally:artifacts.close()


def test_persistent_metadata_state_lives_outside_checkout_and_uploads_from_that_path():
    import yaml
    root=Path(__file__).parents[1]/'.github/workflows'
    for path in root.glob('catalogue_*.yml'):
        for job_name,job in yaml.safe_load(path.read_text())['jobs'].items():
            if job_name=='schema':continue
            assert '/var/lib/findmyrom/catalogue-v3' in job['env']['FMR_CATALOGUE_STATE_DIR']
            assert 'vars.FMR_CATALOGUE_STATE_DIR' in job['env']['FMR_CATALOGUE_STATE_DIR']
            for step in job['steps']:
                if step.get('uses')=='actions/upload-artifact@v4':
                    assert step['with']['path'].startswith('${{ env.FMR_CATALOGUE_STATE_DIR }}/')


def test_local_state_accepts_own_uploaded_checkpoint_without_downloading_it(env,tmp_path,monkeypatch):
    path=tmp_path/'vendor.sqlite';db=staged.open_local(path);db.close()
    staged.checkpoint_stamp(path,artifact_id=9,writer_run_id='123')
    monkeypatch.setenv('FMR_CATALOGUE_LOCAL_STATE','true');artifacts=staged.Artifacts()
    monkeypatch.setattr(artifacts,'list',lambda *_:[{'id':10,'workflow_run':{'id':123}}])
    monkeypatch.setattr(artifacts.client,'get',lambda *_a,**_kw:pytest.fail('Own upload must not be downloaded again'))
    try:assert artifacts.restore('google',path)=='local'
    finally:artifacts.close()
    assert json.loads(path.with_suffix('.checkpoint.json').read_text())['artifact_id']==10


def test_newer_foreign_checkpoint_refreshes_stale_local_state(env,tmp_path,monkeypatch):
    local=tmp_path/'vendor.sqlite';db=staged.open_local(local);db.close()
    staged.checkpoint_stamp(local,artifact_id=9,writer_run_id='123')
    archive=io.BytesIO()
    with zipfile.ZipFile(archive,'w') as z:z.writestr('google.sqlite.enc',staged.seal(b'newer checkpoint','google'))
    monkeypatch.setenv('FMR_CATALOGUE_LOCAL_STATE','true');artifacts=staged.Artifacts()
    monkeypatch.setattr(artifacts,'list',lambda *_:[{'id':10,'workflow_run':{'id':456}}])
    monkeypatch.setattr(artifacts.client,'get',lambda url:httpx.Response(200,content=archive.getvalue(),request=httpx.Request('GET',url)))
    try:assert artifacts.restore('google',local)==10
    finally:artifacts.close()
    assert local.read_bytes()==b'newer checkpoint'


def test_snapshot_uses_sqlite_backup_so_uncheckpointed_wal_rows_are_saved(env,tmp_path,monkeypatch):
    import sqlite3
    root=tmp_path/'state';root.mkdir();monkeypatch.setattr(staged,'ROOT',root)
    source=root/'wal.sqlite';writer=sqlite3.connect(source)
    writer.execute('PRAGMA journal_mode=WAL');writer.execute('CREATE TABLE proof(value TEXT)')
    writer.execute("INSERT INTO proof VALUES ('committed but still in WAL')");writer.commit()
    try:staged.encrypt_file('google',source)
    finally:writer.close()
    restored=tmp_path/'restored.sqlite';restored.write_bytes(staged.unseal((root/'upload/google.sqlite.enc').read_bytes(),'google'))
    with sqlite3.connect(restored) as db:assert db.execute('SELECT value FROM proof').fetchone()[0]=='committed but still in WAL'


def test_completed_migration_marker_prevents_reimport_when_baseline_expires(env,tmp_path,monkeypatch):
    import yaml
    workflow=yaml.safe_load((Path(__file__).parents[1]/'docs/archive/workflows/catalogue_bootstrap.yml.disabled').read_text())
    step=next(item for item in workflow['jobs']['restore']['steps'] if item.get('id')=='baseline')
    code='\n'.join(step['run'].splitlines()[1:-1])
    class Artifacts:
        def list(self,_name):return []
        def close(self):pass
    monkeypatch.setattr(staged,'Artifacts',Artifacts)
    monkeypatch.setenv('FMR_CATALOGUE_MIGRATED','true')
    output=tmp_path/'output';monkeypatch.setenv('GITHUB_OUTPUT',str(output))
    exec(compile(code,'bootstrap-check','exec'),{})
    assert output.read_text()=='needed=false\n'


def test_publish_snapshot_pages_preserve_existing_key_order(env):
    db, _ = env
    store = Store(db, 'google')
    for number in range(7):
        store.put(store.key('release', str(number)), {'status': 'resolved'})
    store.put(store.key('model', 'pixel'), {'status': 'ok'})
    other = Store(db, 'xiaomi')
    other.put(other.key('release', 'other'), {'status': 'resolved'})
    assert list(store.iter_rows('release', page_size=2)) == store.rows('release')



def test_unchanged_snapshot_skips_history_and_new_row_reopens_it(env,tmp_path,monkeypatch):
    local,_=env
    root=tmp_path/'state';root.mkdir()
    path=root/'google.sqlite'
    with sqlite3.connect(path) as snapshot:
        local._get_sqlite_conn().backup(snapshot)
    store=Store(publisher.open_local(path,initialize=False),'google')
    ledger=publisher.open_ledger(root/'publisher.sqlite')
    device={'vendor':'google','codename':'pixel'}
    for number in range(32):
        key=store.key('model',str(number))
        store.put(key,{'device':device})
        ledger.execute(publisher.ACK_SQL,('google',key,publisher.digest(device)))
    store.db.close();ledger.close()
    class Artifacts:
        def restore(self,*args,**kwargs):return True
        def close(self):pass
    monkeypatch.setattr(publisher,'Artifacts',Artifacts)
    monkeypatch.setattr(publisher,'ROOT',root)
    monkeypatch.setattr(publisher,'VENDORS',('google',))
    monkeypatch.setenv('FMR_CATALOGUE_PUBLISH_FORCE','true')
    monkeypatch.setenv('FMR_CATALOGUE_SKIP_EXPORT','true')
    opened=[]
    original=publisher.open_local
    def open_snapshot(*args,**kwargs):
        opened.append(1)
        return original(*args,**kwargs)
    monkeypatch.setattr(publisher,'open_local',open_snapshot)
    calls=[]
    def remote(**kwargs):
        calls.append(1)
        raise ConnectionError('simulated D1 unavailable')
    monkeypatch.setattr(publisher,'BudgetDB',remote)
    for name in ('CF_ACCOUNT_ID','CF_API_TOKEN','CF_D1_DATABASE_ID'):
        monkeypatch.setenv(name,'synthetic')
    assert publisher.main()==0
    assert publisher.main()==0
    assert len(opened)==2 and not calls
    changed=Store(original(path,initialize=False),'google')
    new_key=changed.key('model','new')
    changed.put(new_key,{'device':device})
    changed.db.close()
    for _ in range(2):
        with pytest.raises(ConnectionError,match='simulated D1 unavailable'):
            publisher.main()
    assert len(opened)==4 and len(calls)==2
    class SmallBatch:
        rows_read=rows_written=0
        _fmr_schema_ready=5
        def __init__(self,**kwargs):pass
        def acquire(self):return True
        def settle(self):pass
        def close(self):pass
    published=[]
    def publish_device(db, payload):
        published.append(payload)
    monkeypatch.setattr(publisher,'BudgetDB',SmallBatch)
    monkeypatch.setattr(publisher,'publish_device',publish_device)
    assert publisher.main()==0
    assert published==[device]
    assert publisher.main()==0
    assert len(opened)==6 and published==[device]
    ledger=publisher.open_ledger(root/'publisher.sqlite')
    try:
        assert ledger.query('SELECT key FROM published WHERE key=?',(new_key,))==[{'key':new_key}]
        assert ledger.query('SELECT COUNT(*) AS n FROM published')==[{'n':33}]
    finally:ledger.close()


def test_change_queue_reads_only_changed_keys_and_preserves_concurrent_update(env,tmp_path):
    local,_=env
    store=Store(local,'google')
    device={'vendor':'google','codename':'pixel'}
    for i in range(32):
        store.put(store.key('model',str(i)),{'device':device})
    initial=list(store.publication_rows('model'))
    assert len(initial)==32
    for key,_,revision in initial:store.acknowledge_change(key,revision)
    assert list(store.publication_rows('model'))==[]
    key=initial[0][0]
    store.put(key,{'device':dict(device,name='changed')})
    changed=list(store.publication_rows('model'))
    assert len(changed)==1 and changed[0][0]==key
    store.put(key,{'device':dict(device,name='changed again')})
    store.acknowledge_change(key,changed[0][2])
    remaining=list(store.publication_rows('model'))
    assert len(remaining)==1 and remaining[0][1]['device']['name']=='changed again'


def test_change_queue_normalization_upgrade_reuses_existing_records(env,monkeypatch):
    local,_=env
    from fmr_ingest_stock import release_metadata
    store=Store(local,'google')
    key=store.key('release','build')
    store.put(key,{'device':{'vendor':'google'},'release':{'version':'one'},'status':'resolved'})
    row=list(store.publication_rows('release'))[0]
    store.acknowledge_change(row[0],row[2])
    assert list(Store(local,'google').publication_rows('release'))==[]
    monkeypatch.setattr(release_metadata,'NORMALIZATION_REVISION',release_metadata.NORMALIZATION_REVISION+1)
    rows=list(Store(local,'google').publication_rows('release'))
    assert len(rows)==1 and rows[0][0]==key
    assert store.get(key)['release']['version']=='one'


def test_shard_merge_preserves_newer_receipt_and_reuses_unchanged_shard(tmp_path):
    from fmr_ingest_stock.merge_shards import merge_one

    main = publisher.open_ledger(tmp_path/'publisher.sqlite')
    path = tmp_path/'shard.sqlite'
    shard = publisher.open_ledger(path)
    try:
        main.execute_batch(publisher.acknowledgement('xiaomi','shared','latest'))
        stamp = main.query('SELECT confirmed_at FROM publication_ack_stamp')[0]['confirmed_at']
        shard.execute('INSERT INTO published VALUES (?,?,?)',('xiaomi','shared','old-copy'))
        shard.execute('INSERT INTO published VALUES (?,?,?)',('xiaomi','missing','legacy-proof'))
        assert merge_one(main._get_sqlite_conn(),path) == 1
        assert merge_one(main._get_sqlite_conn(),path) is None
        assert main.query("SELECT hash FROM published WHERE key='shared'")[0]['hash'] == 'latest'
        shard.execute_batch([
            (publisher.ACK_SQL,('xiaomi','shared','fresh')),
            (publisher.ACK_STAMP_SQL,('xiaomi','shared','fresh',stamp+1)),
            (publisher.IDENTITY_REVIEW_SQL,('conflict','fingerprint',100)),
        ])
        merge_one(main._get_sqlite_conn(),path)
        assert main.query("SELECT hash FROM published WHERE key='shared'")[0]['hash'] == 'fresh'
        assert main.query('SELECT key FROM xiaomi_publication_review') == [{'key':'conflict'}]
    finally:
        shard.close()
        main.close()



def test_acknowledged_queue_cleanup_is_batched_and_flushes_when_batch_stops(env,tmp_path):
    local,_=env
    ledger=publisher.open_ledger(tmp_path/'publisher.sqlite')
    store=Store(local,'google')
    device={'vendor':'google','codename':'pixel'}
    keys=sorted(store.key('model',str(i)) for i in range(260))
    for key in keys:
        store.put(key,{'device':device})
    ledger.execute_batch([(publisher.ACK_SQL,('google',key,publisher.digest(device))) for key in keys[:-1]])
    batches=[]
    execute_batch=local.execute_batch
    def tracked(statements):
        batches.append(len(statements))
        return execute_batch(statements)
    local.execute_batch=tracked
    stream=publisher.candidates(local,ledger,'google')
    assert next(stream)[1]==keys[-1]
    # Closing at the record limit flushes the last partial group before SQLite closes.
    stream.close()
    assert local.query('SELECT key FROM publication_changes')==[{'key':keys[-1]}]
    cleanup=[n for n in batches if n==128 or n==3]
    assert cleanup==[128,128,3]
    assert ledger.query('SELECT COUNT(*) AS n FROM published')==[{'n':259}]
    ledger.close()


@pytest.mark.parametrize('vendor',['google','samsung','xiaomi'])
def test_identity_conflict_defers_unchanged_payload_but_not_corrections(env,tmp_path,monkeypatch,vendor):
    from collections import Counter
    local,_=env
    ledger=publisher.open_ledger(tmp_path/'publisher.sqlite')
    store=Store(local,vendor)
    key=store.key('release','one')
    value={'status':'resolved','device':{'vendor':vendor,'codename':'sample'},
           'release':{'version':'1','download_url':'https://example.test/one.zip'}}
    store.put(key,value)
    candidate=list(publisher.candidates(local,ledger,vendor))[0]
    monkeypatch.setattr(publisher.time,'time',lambda:1000)
    def conflict(*args):
        raise publisher.FirmwareIdentityPending('Different file')
    monkeypatch.setattr(publisher,'publish_release',conflict)
    assert publisher.sync_one(None,ledger,vendor,candidate) is False
    stats=Counter()
    assert list(publisher.candidates(local,ledger,vendor,stats))==[]
    assert stats['identity_review_deferred']==1
    assert ledger.query('SELECT * FROM published')==[]
    assert len(list(publisher.candidates(local,ledger,vendor,respect_review=False)))==1
    value['release']['download_url']='https://example.test/corrected.zip'
    store.put(key,value)
    assert len(list(publisher.candidates(local,ledger,vendor)))==1
    value['release']['download_url']='https://example.test/one.zip'
    store.put(key,value)
    assert list(publisher.candidates(local,ledger,vendor))==[]
    monkeypatch.setattr(publisher.time,'time',lambda:1001+publisher.IDENTITY_REVIEW_DELAY)
    assert len(list(publisher.candidates(local,ledger,vendor)))==1
    ledger.close()
