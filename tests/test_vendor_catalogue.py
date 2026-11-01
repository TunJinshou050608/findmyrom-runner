"""Real SQLite/checkpoints/cache/crypto; HTTP is synthetic metadata only."""
import json
from pathlib import Path
import threading
import httpx
import pytest
import yaml

from fmr_core.d1_client import D1Client
from fmr_core.crypto import generate_keypair
from fmr_ingest_stock.catalogue_store import Store,KIND
from fmr_ingest_stock.catalogue_cache import CachedClient
from fmr_ingest_stock import vendor_catalogue as job
from fmr_ingest_stock.catalogue_job import save as legacy_save,task_key


@pytest.fixture
def env(tmp_path,monkeypatch):
    from fmr_ingest_stock.device_sync import ensure_base_brands_and_anchor
    path=str(tmp_path/'private.sqlite')
    db=D1Client(local_sqlite_path=path)
    db.execute_script(Path(__file__).parents[1].joinpath('schema.sql').read_text())
    ensure_base_brands_and_anchor(db)
    key,_=generate_keypair()
    for k,v in {'GITHUB_RUN_ID':'123','GITHUB_RUN_ATTEMPT':'1','GITHUB_REF_NAME':'main',
                'GITHUB_REPOSITORY':'owner/runner','GITHUB_SHA':'a'*40,'GH_WORKFLOW_TOKEN':'ephemeral',
                'SIGN_PRIVATE_KEY_HEX':key,'GITHUB_STEP_SUMMARY':str(tmp_path/'summary.md')}.items():monkeypatch.setenv(k,v)
    yield db,path
    db.close()


class Source:
    def __init__(self,models=5):
        self.models=models;self.changed=False;self.requests=[];self.dispatches=[];self.fail=False
    def __call__(self,req):
        self.requests.append(req)
        if req.url.host=='api.github.com':
            self.dispatches.append(json.loads(req.content))
            assert req.url.path.endswith('/catalogue_oneplus.yml/dispatches')
            return httpx.Response(204)
        assert req.url.host=='fwatlas.com','A metadata job requested firmware bytes'
        path=req.url.path
        tag=('"models-'+str(self.models)+'"') if path=='/oneplus' else '"v2"' if self.changed and path=='/oneplus/sample-0' else '"v1"'
        if req.headers.get('If-None-Match')==tag:return httpx.Response(304)
        if path=='/oneplus':
            body=''.join('<a href="/oneplus/sample-'+str(n)+'">Sample</a>' for n in range(self.models))
        elif path.endswith(('/build-1','/build-2')):
            if self.fail:return httpx.Response(503)
            version='BUILD.2' if path.endswith('build-2') else 'BUILD.1'
            body='<h1>'+version+'</h1><a href="https://ota.oneplus.net/'+version+'.zip">Download</a>'
        else:
            builds=[1,2] if self.changed and path=='/oneplus/sample-0' else [1]
            body='<table><tbody>'+''.join('<tr data-region="EU"><td><a href="'+path+'/build-'+str(n)+'"><code>BUILD.'+str(n)+'</code></a></td><td data-label="Type">Full OTA</td></tr>' for n in builds)+'</tbody></table>'
        return httpx.Response(200,headers={'Content-Type':'text/html','ETag':tag},content=body)


def clients(monkeypatch,source):
    real=httpx.Client
    monkeypatch.setattr(job.httpx,'Client',lambda *a,**kw:real(*a,**kw,transport=httpx.MockTransport(source)))


def finish(db,cycle,vendor='oneplus'):
    store=Store(db,vendor)
    for _ in range(12):
        c=store.get(store.key('control'))
        if c['state']!='working':return c
        job.run(db,vendor,cycle=cycle,sequence=c['batch'])
    raise AssertionError('Autonomous continuation did not terminate')


def test_independent_workflows_are_single_vendor_named_and_no_mixed_dispatch():
    root=Path(__file__).parents[1]/'.github/workflows'
    for vendor,label in job.LABELS.items():
        data=yaml.safe_load((root/f'catalogue_{vendor}.yml').read_text())
        assert label in data['name'] and '增量更新' in data['run-name']
        assert data['concurrency']['group']=='catalogue-v2-'+vendor
        assert data['concurrency']['cancel-in-progress'] is False
        step=next(s for s in data['jobs']['catalogue']['steps'] if s.get('id')=='collect')
        assert '--vendor '+vendor+' ' in step['run'] and '--brands' not in step['run']
        assert 'CF_API_TOKEN' not in str(data) and 'CF_D1_DATABASE_ID' not in str(data)
        assert '30' in step['name'] and 'GH_VAULT_TOKEN' not in step['env']
        triggers=data.get('on') or data[True]
        if vendor in ('huawei','honor'):assert 'schedule' not in triggers
        else:assert 'schedule' in triggers


def test_cross_process_resume_and_immediate_next_run_do_not_refetch_completed_data(env,monkeypatch):
    db,path=env;source=Source();clients(monkeypatch,source)
    assert job.run(db,'oneplus')==0
    store=Store(db,'oneplus');c=store.get(store.key('control'))
    assert c['state']=='working' and c['batch']==2
    assert source.dispatches[0]['inputs']['cycle']==c['cycle']
    # New process / fresh DB connection, retaining only durable state.
    reopened=D1Client(local_sqlite_path=path)
    result=finish(reopened,c['cycle']);reopened.close()
    assert result['state']=='completed'
    calls=len(source.requests)
    monkeypatch.setenv('GITHUB_RUN_ID','124')
    assert job.run(db,'oneplus')==0
    assert len(source.requests)==calls # No source or continuation requests.
    assert Store(db,'oneplus').counts()=={'models':{'ok':5},'releases':{'resolved':5}}
    assert not db.query('SELECT * FROM stock_firmwares')


def test_changed_source_only_resolves_new_release_and_uses_304_for_unchanged(env,monkeypatch):
    db,_=env;source=Source();clients(monkeypatch,source)
    job.run(db,'oneplus');store=Store(db,'oneplus');finish(db,store.get(store.key('control'))['cycle'])
    old_detail_calls=sum(r.url.path.endswith('build-1') for r in source.requests)
    future=job.time.time()+job.MODEL_TTL+10
    monkeypatch.setattr(job.time,'time',lambda:future)
    source.changed=True;monkeypatch.setenv('GITHUB_RUN_ID','125')
    job.run(db,'oneplus');finish(db,store.get(store.key('control'))['cycle'])
    assert store.counts()['releases']=={'resolved':6}
    assert sum(r.url.path.endswith('build-1') for r in source.requests)==old_detail_calls
    assert sum(r.url.path.endswith('build-2') for r in source.requests)==1
    assert any('If-None-Match' in r.headers for r in source.requests)


def test_audit_results_can_be_published_without_refetch_and_repeat_publish_preserves_tokens(env,monkeypatch):
    db,_=env;source=Source(1);clients(monkeypatch,source)
    assert job.run(db,'oneplus')==0
    n=len(source.requests);monkeypatch.setenv('GITHUB_RUN_ID','126')
    assert job.run(db,'oneplus',mode='publish')==0
    rows=db.query('SELECT * FROM stock_firmwares');assert len(rows)==1
    token=rows[0]['public_token'];signature=rows[0]['ed25519_signature']
    assert rows[0]['extract_status']=='skipped' and len(source.requests)==n
    monkeypatch.setenv('GITHUB_RUN_ID','127');job.run(db,'oneplus',mode='publish')
    again=db.query('SELECT * FROM stock_firmwares')[0]
    assert again['public_token']==token and again['ed25519_signature']==signature
    assert len(source.requests)==n


def test_migration_reuses_completed_legacy_models_and_resolved_links_without_network(env,monkeypatch):
    db,_=env;source=Source(1);clients(monkeypatch,source)
    device={'vendor':'oneplus','upstream_id':'sample','name':'Sample','page':'https://fwatlas.com/oneplus/sample'}
    row={'vendor':'oneplus','device_id':'sample','version':'BUILD.1','region':'EU','kind':'Full OTA',
         'detail_page':'https://fwatlas.com/oneplus/sample/build-1','download_url':'https://ota.oneplus.net/BUILD.1.zip'}
    legacy_save(db,task_key('oem_legacy','model','sample'),{'device':device,'done':True,'provenance':{}})
    legacy_save(db,task_key('oem_legacy','release','sample'),{'device':device,'release':row,'done':True})
    assert job.run(db,'oneplus')==0
    assert source.requests==[]
    assert Store(db,'oneplus').counts()=={'models':{'ok':1},'releases':{'resolved':1}}
    assert len(db.query("SELECT * FROM crawler_sources WHERE source_kind='oem_catalogue'"))==2


def test_failed_entries_are_bounded_and_explicit_retry_does_not_repeat_good_models(env,monkeypatch):
    db,_=env;source=Source(1);source.fail=True;clients(monkeypatch,source)
    job.run(db,'oneplus');store=Store(db,'oneplus');cycle=store.get(store.key('control'))['cycle']
    result=finish(db,cycle)
    assert result['state']=='completed_with_gaps' and store.counts()['releases']=={'failed':1}
    model_calls=sum(r.url.path=='/oneplus/sample-0' for r in source.requests)
    source.fail=False;monkeypatch.setenv('GITHUB_RUN_ID','128')
    assert job.run(db,'oneplus',operation='retry_failed')==0
    assert store.counts()['releases']=={'resolved':1}
    assert sum(r.url.path=='/oneplus/sample-0' for r in source.requests)==model_calls


def test_stale_and_duplicate_continuations_do_not_dispatch_or_modify_progress(env,monkeypatch):
    db,_=env;source=Source();clients(monkeypatch,source)
    job.run(db,'oneplus');store=Store(db,'oneplus');before=store.get(store.key('control'))
    n=len(source.requests)
    assert job.run(db,'oneplus',cycle='c999',sequence=1)==0
    assert job.run(db,'oneplus',cycle=before['cycle'],sequence=1)==0
    assert store.get(store.key('control'))==before and len(source.requests)==n


def test_cache_reuse_conditional_requests_and_binary_guards(env):
    db,_=env;store=Store(db,'google');clock=[10000];calls=[]
    class Never(httpx.SyncByteStream):
        def __iter__(self):raise AssertionError('Binary body consumed');yield b''
    def handler(req):
        calls.append(req)
        if req.url.path=='/binary':return httpx.Response(200,headers={'Content-Type':'application/zip'},stream=Never())
        if req.headers.get('If-None-Match')=='"same"':return httpx.Response(304)
        return httpx.Response(200,headers={'Content-Type':'text/html','ETag':'"same"'},content=b'public metadata')
    with httpx.Client(transport=httpx.MockTransport(handler)) as raw:
        cache=CachedClient(store,raw,now=lambda:clock[0])
        for _ in range(2):
            with cache.stream('GET','https://developers.google.com/android/ota') as r:assert r.read()==b'public metadata'
        assert len(calls)==1
        clock[0]+=3601
        # Simulate a new process, not merely an in-memory cache hit.
        cache=CachedClient(store,raw,now=lambda:clock[0])
        with cache.stream('GET','https://developers.google.com/android/ota') as r:assert r.read()==b'public metadata'
        assert cache.stats['not_modified']==1 and len(calls)==2
        with pytest.raises(ValueError):
            with cache.stream('GET','https://fwatlas.com/rom.zip'):pass
        with pytest.raises(ValueError):
            with cache.stream('GET','https://fwatlas.com/binary'):pass


def test_human_status_heartbeat_and_summary_explain_progress_without_source_data(tmp_path,capsys):
    path=tmp_path/'summary.md'
    with job.Status('oneplus',str(path),interval=0.01) as status:
        status.update('正在检查来源',counts={'models':{'ok':2},'releases':{'resolved':4}},checkpoint=True)
        # Event wait bounds this test; it does not touch any live Actions run.
        threading.Event().wait(0.04)
    output=capsys.readouterr().out
    assert '仍在执行' in output and '最近检查点' in output
    assert '| 固件记录 | 链接已解析 | 4 |' in path.read_text()
    assert 'http' not in output and '没有执行真实镜像' in path.read_text()


def test_new_models_are_added_without_refetching_fresh_existing_models(env,monkeypatch):
    db,_=env;source=Source(1);clients(monkeypatch,source)
    job.run(db,'oneplus');old_model_calls=sum(r.url.path=='/oneplus/sample-0' for r in source.requests)
    future=job.time.time()+3601;monkeypatch.setattr(job.time,'time',lambda:future)
    source.models=2;monkeypatch.setenv('GITHUB_RUN_ID','129')
    assert job.run(db,'oneplus')==0
    assert Store(db,'oneplus').counts()['models']=={'ok':2}
    assert sum(r.url.path=='/oneplus/sample-0' for r in source.requests)==old_model_calls


def test_interruption_after_fanout_keeps_seeded_releases_and_cached_model_page(env,monkeypatch):
    db,_=env;source=Source(1);clients(monkeypatch,source)
    # Real DB failure at a precise checkpoint boundary, after source data and
    # release fanout were saved but before the parent model could be marked done.
    db.execute_script("""CREATE TRIGGER simulate_interruption BEFORE INSERT ON crawler_sources
      WHEN NEW.source_kind='oem_incremental_v2' AND NEW.source_target GLOB 'v2:oneplus:model:*'
      AND json_extract(NEW.last_cursor_val,'$.status')='ok'
      BEGIN SELECT RAISE(FAIL,'simulated checkpoint interruption'); END;""")
    assert job.run(db,'oneplus')==1
    store=Store(db,'oneplus')
    assert store.get(store.key('control'))['state']=='paused'
    assert store.counts()['releases']=={'pending':1}
    old=sum(r.url.path=='/oneplus/sample-0' for r in source.requests)
    db.execute('DROP TRIGGER simulate_interruption')
    assert job.run(db,'oneplus')==0
    assert store.counts()['releases']=={'resolved':1}
    assert sum(r.url.path=='/oneplus/sample-0' for r in source.requests)==old


def test_rejected_continuation_can_resume_from_original_failed_batch(env,monkeypatch):
    db,_=env;source=Source();fail=[True]
    def handler(req):
        if req.url.host=='api.github.com' and fail[0]:
            fail[0]=False;return httpx.Response(503)
        return source(req)
    clients(monkeypatch,handler)
    assert job.run(db,'oneplus')==1
    store=Store(db,'oneplus');c=store.get(store.key('control'))
    assert c['state']=='paused' and c['batch']==2 and c['continuation_pending']
    assert job.run(db,'oneplus',cycle=c['cycle'],sequence=1)==0
    assert store.get(store.key('control'))['state']=='completed'
    assert store.counts()['releases']=={'resolved':5}


def test_xiaomi_history_reuses_persistent_detail_pages_across_refreshes(env,monkeypatch):
    from test_history_sweep import detail
    db,_=env;requests=[]
    def handler(req):
        requests.append(req)
        assert req.url.host in ('raw.githubusercontent.com','xmfirmwareupdater.com')
        if req.headers.get('If-None-Match')=='"same"':return httpx.Response(304)
        if req.url.host=='raw.githubusercontent.com':body='sample: [Sample]\n';media='text/plain'
        elif req.url.path=='/archive/miui/sample/':body='<a href="/miui/sample/stable/V1/">Version</a>';media='text/html'
        elif req.url.path=='/archive/hyperos/sample/':body='<html>No HyperOS</html>';media='text/html'
        else:body=detail();media='text/html'
        return httpx.Response(200,headers={'Content-Type':media,'ETag':'"same"'},content=body)
    clients(monkeypatch,handler)
    assert job.run(db,'xiaomi')==0
    assert Store(db,'xiaomi').counts()=={'models':{'ok':1},'releases':{'resolved':1}}
    old=sum(r.url.path.startswith('/miui/') for r in requests)
    future=job.time.time()+job.MODEL_TTL+1;monkeypatch.setattr(job.time,'time',lambda:future)
    monkeypatch.setenv('GITHUB_RUN_ID','130')
    assert job.run(db,'xiaomi')==0
    assert sum(r.url.path.startswith('/miui/') for r in requests)==old
    assert any('If-None-Match' in r.headers for r in requests)


def test_batch_deadline_schedules_remaining_work_without_burning_failure_attempts(env):
    from fmr_ingest_stock.oem_sources import MetadataHTTP
    db,_=env;store=Store(db,'oneplus')
    device={'vendor':'oneplus','upstream_id':'sample','page':'https://fwatlas.com/oneplus/sample'}
    job.seed_models(store,[device],{})
    with httpx.Client(transport=httpx.MockTransport(lambda req:pytest.fail('Deadline should avoid a request'))) as raw:
        cache=CachedClient(store,raw);cache.deadline=0
        http=MetadataHTTP(cache)
        with job.Status('oneplus') as status:
            more,counts,changed=job.batch(store,{'cycle':'c123','operation':'incremental','mode':'audit'},http,status)
    assert more and changed==0 and counts['models']=={'pending':1}
    assert store.rows('model')[0][1]['attempts']==0


def test_github_rerun_failed_jobs_retries_gaps_instead_of_silently_skipping(env,monkeypatch):
    db,_=env;source=Source(1);source.fail=True;clients(monkeypatch,source)
    job.run(db,'oneplus');store=Store(db,'oneplus');old=store.get(store.key('control'))
    finish(db,old['cycle']);completed=store.get(store.key('control'))
    assert completed['state']=='completed_with_gaps'
    source.fail=False;monkeypatch.setenv('GITHUB_RUN_ATTEMPT','2')
    assert job.run(db,'oneplus',cycle=old['cycle'],sequence=completed['batch']-1)==0
    after=store.get(store.key('control'))
    assert after['state']=='completed' and after['cycle']!=old['cycle']
    assert store.counts()['releases']=={'resolved':1}


def test_corrupt_page_cache_is_rebuilt_without_erasing_catalogue_tasks(env):
    db,_=env;store=Store(db,'google');url='https://developers.google.com/android/ota'
    key=store.key('page',[url,{}])
    store.put(key,{'body':'broken','digest':'bad','checked_at':9999999999,'headers':{'Content-Type':'text/html'}})
    device={'vendor':'google','upstream_id':'sample'}
    job.seed_models(store,[device],{})
    with httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(200,headers={'Content-Type':'text/html'},content=b'rebuilt'))) as raw:
        cache=CachedClient(store,raw)
        with cache.stream('GET',url) as response:assert response.read()==b'rebuilt'
    assert store.counts()['models']=={'pending':1}
    assert CachedClient.unpack(store.get(key))==b'rebuilt'



def test_adapter_revision_retries_only_affected_records_once_and_excludes_chromeos(env):
 from fmr_ingest_stock.catalogue_publish import candidates,open_ledger
 db,path=env;store=Store(db,'asus')
 phone={'vendor':'asus','upstream_id':'phone','name':'Phone'};pc={'vendor':'asus','upstream_id':'chromebook','name':'Chromebook'}
 for device in (phone,pc):store.put(store.key('model',device['upstream_id']),{'device':device,'status':'ok'})
 entries=[('bad',phone,'full','blocked','https://dlcdnets.asus.com/bad.zip'),('good',phone,'full','resolved','https://dlcdnets.asus.com/good.zip'),('chrome',pc,'chromeos','blocked','')]
 for key,device,kind,state,url in entries:store.put(store.key('release',key),{'device':device,'release':{'vendor':'asus','kind':kind,'download_url':url},'status':state,'next_check':2**40,'last_cycle':'old'})
 with job.Status('asus',interval=100) as status:job.upgrade_adapters(store,status)
 assert store.get(store.key('release','bad'))['status']=='pending'
 assert store.get(store.key('release','good'))['status']=='resolved'
 assert store.get(store.key('release','chrome'))['status']=='excluded'
 assert store.get(store.key('model','chromebook'))['status']=='excluded'
 assert not any(v['status']=='excluded' for _,v in store.due('release','new',10,operation='retry_failed'))
 store.put(store.key('release','bad'),{**store.get(store.key('release','bad')),'status':'blocked','next_check':2**40})
 with job.Status('asus',interval=100) as status:job.upgrade_adapters(store,status)
 assert store.get(store.key('release','bad'))['status']=='blocked'
 ledger=open_ledger(Path(path).with_suffix('.publisher'))
 try:assert all(v['device']['upstream_id']!='chromebook' for _,_,v,_ in candidates(db,ledger,'asus'))
 finally:ledger.close()


def test_404_is_terminal_for_current_cycle_and_cannot_immediately_retry_three_times(env):
 db,_=env;row={'status':'pending'}
 job.mark_problem(row,'HTTP_404','cycle')
 assert row['status']=='blocked' and row['attempts']==1 and row['last_cycle']=='cycle'
 assert row['next_check']>job.time.time()


def test_existing_samsung_fota_queue_is_repaired_without_any_network(env):
 db,_=env;store=Store(db,'samsung')
 device={'vendor':'samsung','upstream_id':'sample','name':'Sample'}
 key=store.key('release','fota')
 store.put(key,{'device':device,'release':{'vendor':'samsung','version':'PDA/CSC/PHONE',
  'metadata_source':'samsung_fota_version_xml','detail_page':'https://fwatlas.com/samsung/sample/guessed'},'status':'retry','attempts':1})
 with job.Status('samsung',interval=100) as status:job.upgrade_adapters(store,status)
 value=store.get(key)
 assert value['status']=='blocked' and value['error']=='SamsungFotaNeedsDownloadResolver'
 assert 'detail_page' not in value['release']
 assert not store.due('release','new',1)


def test_fota_only_samsung_references_stay_out_of_even_forced_web_retry_queue(env):
 db,_=env;store=Store(db,'samsung');key=store.key('release','fota')
 store.put(key,{'device':{'vendor':'samsung','upstream_id':'sample'},'release':{'resolution':'requires_samsung_fus'},'status':'blocked','next_check':0})
 assert not store.due('release','new',10,operation='retry_failed')
