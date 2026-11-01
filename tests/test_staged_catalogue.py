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
        if path.name=='catalogue_publish.yml':
            assert data[True]['schedule'][0]['cron']=='*/10 * * * *'
            assert data['concurrency']['group']=='catalogue-d1-publisher'
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
        rows_read=1;rows_written=1
        def __init__(self,**kwargs):pass
        def acquire(self):return True
        def settle(self):raise RuntimeError('simulated settlement outage')
        def close(self):closed.append('database')
    def synced(db,ledger,vendor,item):
        ledger.execute('INSERT INTO published(vendor,key,hash) VALUES (?,?,?)',('google','successful-remote-item','hash'))
        return True
    monkeypatch.setattr(publisher,'Artifacts',Artifacts);monkeypatch.setattr(publisher,'VENDORS',('google',))
    monkeypatch.setattr(publisher,'open_local',lambda _:Local())
    monkeypatch.setattr(publisher,'candidates',lambda *_:iter([('synthetic',)]))
    monkeypatch.setattr(publisher,'BudgetDB',DB);monkeypatch.setattr(publisher,'sync_one',synced)
    for name in ('CF_ACCOUNT_ID','CF_API_TOKEN','CF_D1_DATABASE_ID'):monkeypatch.setenv(name,'synthetic')
    with pytest.raises(RuntimeError,match='settlement outage'):publisher.main()
    restored=tmp_path/'restored.sqlite'
    restored.write_bytes(staged.unseal((root/'upload/publisher.sqlite.enc').read_bytes(),'publisher'))
    ledger=publisher.open_ledger(restored)
    try:assert ledger.query('SELECT key FROM published')==[{'key':'successful-remote-item'}]
    finally:ledger.close()
    assert closed==['database','artifacts']
