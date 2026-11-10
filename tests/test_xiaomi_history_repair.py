"""Real SQLite checkpoints and the production URL gate; no ROM bytes or D1."""
import json
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest

from fmr_ingest_stock.history import (INDEX,HistoryCheckpoint,discover_device,parse_detail,history_url)
from fmr_ingest_stock.catalogue_store import Store
from fmr_ingest_stock.catalogue_cache import CachedClient,CatalogueDeadline
from fmr_ingest_stock.oem_sources import MetadataHTTP,safe_url
from fmr_ingest_stock.catalogue_sync import publish_release,FirmwareIdentityPending
from fmr_ingest_stock import vendor_catalogue as job
from test_history_sweep import detail
from test_vendor_catalogue import env


def test_raw_space_archive_links_pass_the_real_security_gate(env):
    db,_=env;store=Store(db,'xiaomi');calls=[]
    def route(req):
        calls.append(req.url.raw_path.decode())
        if req.url.path.startswith('/archive/miui/'):
            return httpx.Response(200,text='<a href="/miui/sample/stable beta/V1/">beta</a>')
        if req.url.path.startswith('/archive/hyperos/'):return httpx.Response(404)
        return httpx.Response(200,headers={'Content-Type':'text/html'},text=detail())
    with httpx.Client(transport=httpx.MockTransport(route)) as raw:
        cache=CachedClient(store,raw)
        try:
            rows,m=discover_device('sample',cache,checkpoint=HistoryCheckpoint(store))
            assert len(rows)==1 and not m['errors']
            assert '/miui/sample/stable%20beta/V1/' in calls
        finally:cache.close()
    with pytest.raises(ValueError):safe_url(INDEX+'/miui/sample/stable beta/V1/')
    with pytest.raises(ValueError):history_url('https://attacker.invalid/stable beta/V1/')


@pytest.mark.parametrize('missing',['Unknown','','N/A','-'])
def test_missing_checksum_keeps_trusted_package_without_inventing_digest(missing):
    rows,rejected=parse_detail(detail().replace('a'*32,missing),'sample')
    assert len(rows)==1 and rejected==0 and rows[0]['md5'] is None
    assert not parse_detail(detail().replace('a'*32,'wrong-digest'),'sample')[0]
    assert not parse_detail(detail().replace('bigota.d.miui.com','evil.invalid').replace('a'*32,missing),'sample')[0]


def test_weekly_global_and_china_are_separate_identities():
    html=detail(version='8.10.11',filename='miui_SAMPLEGlobal_8.10.11_aaaaaaaaaa_8.1.zip')+detail(version='8.10.11',filename='miui_SAMPLE_8.10.11_bbbbbbbbbb_8.1.zip')
    rows,_=parse_detail(html,'sample')
    assert {r['codename'] for r in rows}=={'sample','sample_global'}


def test_bounded_resume_survives_html_eviction_and_only_retries_failed_page(env,monkeypatch):
    db,_=env;store=Store(db,'xiaomi');calls=[];broken=True
    def route(req):
        nonlocal broken
        path=req.url.path;calls.append(path)
        if path.startswith('/archive/miui/'):
            return httpx.Response(200,text=''.join(f'<a href="/miui/sample/stable/V{i}/">v</a>' for i in range(3)))
        if path.startswith('/archive/hyperos/'):return httpx.Response(404)
        if path.endswith('/V1/') and broken:return httpx.Response(404)
        version='V'+path.split('/')[-2][1:]+'.0.0.0.TXXMIXM'
        return httpx.Response(200,headers={'Content-Type':'text/html'},text=detail(version=version))
    with httpx.Client(transport=httpx.MockTransport(route)) as raw:
        checkpoint=HistoryCheckpoint(store)
        rows,m=discover_device('sample',raw,checkpoint=checkpoint,max_details=1,max_workers=1)
        assert len(rows)==1 and m['pending_pages']==2
        rows,m=discover_device('sample',raw,checkpoint=HistoryCheckpoint(Store(db,'xiaomi')),max_details=1,max_workers=1)
        assert len(rows)==1 and m['pending_pages']==1 and m['errors'][0]['status']==404
        rows,m=discover_device('sample',raw,checkpoint=checkpoint,max_details=1,max_workers=1)
        assert len(rows)==2 and m['pending_pages']==0 and m['errors']
        # No HTML cache or process memory is needed for successful parsed pages.
        before=len([p for p in calls if not p.startswith('/archive/')])
        rows,m=discover_device('sample',raw,checkpoint=HistoryCheckpoint(Store(db,'xiaomi')),max_details=1,max_workers=1)
        assert len([p for p in calls if not p.startswith('/archive/')])==before
        broken=False;future=time.time()+86401
        monkeypatch.setattr('fmr_ingest_stock.history.time.time',lambda:future)
        rows,m=discover_device('sample',raw,checkpoint=checkpoint,max_details=1,max_workers=1)
        assert len(rows)==3 and not m['errors'] and not m['pending_pages']
        assert calls.count('/miui/sample/stable/V0/')==1
        assert calls.count('/miui/sample/stable/V2/')==1
        assert calls.count('/miui/sample/stable/V1/')==2


def test_successful_source_evidence_survives_refresh_failure(env,monkeypatch):
    db,_=env;store=Store(db,'xiaomi');broken=False
    def route(req):
        if req.url.path.startswith('/archive/miui/'):return httpx.Response(200,text='<a href="/miui/sample/stable/V1/">v</a>')
        if req.url.path.startswith('/archive/hyperos/'):return httpx.Response(404)
        return httpx.Response(404) if broken else httpx.Response(200,text=detail())
    with httpx.Client(transport=httpx.MockTransport(route)) as raw:
        checkpoint=HistoryCheckpoint(store);first,_=discover_device('sample',raw,checkpoint=checkpoint)
        broken=True;future=time.time()+31*86400
        monkeypatch.setattr('fmr_ingest_stock.history.time.time',lambda:future)
        again,m=discover_device('sample',raw,checkpoint=checkpoint)
        assert again==first and m['errors'] and m['retry_after']>future


def test_real_conflict_quarantines_both_files_but_debug_variant_does_not(env):
    db,_=env
    def run(html):
        def route(req):
            if req.url.path.startswith('/archive/miui/'):return httpx.Response(200,text='<a href="/miui/sample/stable/V1/">v</a>')
            if req.url.path.startswith('/archive/hyperos/'):return httpx.Response(404)
            return httpx.Response(200,text=html)
        with httpx.Client(transport=httpx.MockTransport(route)) as raw:return discover_device('sample',raw)
    rows,m=run(detail()+detail(filename='miui_SAMPLE_other_13.0.zip'))
    assert not rows and m['conflicts'][0]['blocking'] and m['conflicts'][0]['kind']=='package_variant'
    rows,m=run(detail(kind='Fastboot')+detail(kind='Fastboot',filename='sample_images_V1.0.0.0.TXXMIXM.debug_13.0_aaaaaaaaaa.tgz'))
    assert len(rows)==1 and '.debug' not in rows[0]['link']
    assert not m['conflicts'][0]['blocking']
    rows,m=run(detail()+detail().replace('bigota.d.miui.com','hugeota.d.miui.com'))
    assert len(rows)==1 and m['conflicts'][0]['kind']=='mirror_alias' and not m['conflicts'][0]['blocking']


def test_legacy_failed_model_upgrade_keeps_existing_release_and_healthy_model(env):
    db,_=env;store=Store(db,'xiaomi')
    bad={'device':{'upstream_id':'air'},'status':'failed','next_check':9999999999,'attempts':3,'provenance':{'errors':[{'error':'ValueError'}]}}
    good={'device':{'upstream_id':'sample'},'status':'ok','next_check':9999999999}
    store.put(store.key('model','air'),bad);store.put(store.key('model','sample'),good)
    release={'device':{'upstream_id':'air'},'release':{'version':'old'},'status':'resolved','published':True}
    store.put(store.key('release','old'),release)
    class Status:
        def log(self,message):pass
    job.upgrade_adapters(store,Status())
    assert store.get(store.key('model','air'))['status']=='pending'
    assert store.get(store.key('model','sample'))==good
    assert store.get(store.key('release','old'))==release
    job.upgrade_adapters(store,Status())
    assert store.get(store.key('release','old'))==release


def test_existing_xiaomi_identity_cannot_be_acknowledged_with_another_archive(env):
    db,_=env;device={'vendor':'xiaomi','upstream_id':'sample','codename':'sample','name':'Sample'}
    release={'version':'8.10.11','region':'CN','android_version':'8.1','kind':'full_ota','download_url':'https://bigota.d.miui.com/8.10.11/miui_SAMPLEGlobal_8.10.11_aaaaaaaaaa_8.1.zip'}
    result=publish_release(db,device,release)
    before=db.query('SELECT * FROM stock_firmwares');metadata=db.query('SELECT * FROM firmware_metadata')
    with pytest.raises(FirmwareIdentityPending):publish_release(db,device,{**release,'download_url':'https://bigota.d.miui.com/8.10.11/miui_SAMPLE_8.10.11_bbbbbbbbbb_8.1.zip'})
    assert db.query('SELECT * FROM stock_firmwares')==before
    assert db.query('SELECT * FROM firmware_metadata')==metadata
    alias={**release,'download_url':release['download_url'].replace('bigota','hugeota')}
    assert publish_release(db,device,alias)['public_token']==result['public_token']


def test_legacy_gap_pages_are_prioritized_and_budget_does_not_burn_attempts(env):
    db,_=env;store=Store(db,'xiaomi');calls=[]
    def route(req):
        path=req.url.path;calls.append(path)
        if path.startswith('/archive/miui/'):
            return httpx.Response(200,text='<a href="/miui/sample/stable/A/">a</a><a href="/miui/sample/stable/Z/">z</a>')
        if path.startswith('/archive/hyperos/'):return httpx.Response(404)
        return httpx.Response(200,text=detail(version='V'+('1' if path.endswith('/A/') else '2')+'.0.0.0.TXXMIXM'))
    with httpx.Client(transport=httpx.MockTransport(route)) as raw:
        old={'errors':[{'url':INDEX+'/miui/sample/stable/Z/','error':'ValueError'}]}
        rows,m=discover_device('sample',raw,checkpoint=HistoryCheckpoint(store),max_details=1,previous=old)
        assert m['pending_pages']==1 and calls[-1].endswith('/Z/')
        assert not m['errors']
        rows,m=discover_device('sample',raw,checkpoint=HistoryCheckpoint(store),max_details=1,previous=m)
        assert len(rows)==2 and not m['pending_pages'] and not m['errors']


def test_partial_invalid_checksum_is_not_silently_marked_complete():
    def route(req):
        if req.url.path.startswith('/archive/miui/'):return httpx.Response(200,text='<a href="/miui/sample/stable/V1/">v</a>')
        if req.url.path.startswith('/archive/hyperos/'):return httpx.Response(404)
        return httpx.Response(200,text=detail()+detail(kind='Fastboot').replace('a'*32,'invalid'))
    with httpx.Client(transport=httpx.MockTransport(route)) as raw:
        rows,m=discover_device('sample',raw)
    assert len(rows)==1 and m['errors'][0]['error']=='RejectedFullROMRecord'


def test_checkpoint_is_saved_before_deadline_and_resume_does_not_refetch(env):
    db,_=env;store=Store(db,'xiaomi');calls=[];stop=True
    class Client:
        @contextmanager
        def stream(self,method,url):
            path=httpx.URL(url).path;calls.append(path)
            if path.startswith('/archive/miui/'):
                body='<a href="/miui/sample/stable/A/">a</a><a href="/miui/sample/stable/Z/">z</a>'
            elif path.startswith('/archive/hyperos/'):
                yield httpx.Response(404,request=httpx.Request(method,url));return
            elif path.endswith('/Z/') and stop:raise CatalogueDeadline()
            else:body=detail(version='V'+('1' if path.endswith('/A/') else '2')+'.0.0.0.TXXMIXM')
            yield httpx.Response(200,text=body,request=httpx.Request(method,url))
    with pytest.raises(CatalogueDeadline):discover_device('sample',Client(),checkpoint=HistoryCheckpoint(store),max_workers=1)
    stop=False
    rows,m=discover_device('sample',Client(),checkpoint=HistoryCheckpoint(Store(db,'xiaomi')),max_workers=1)
    assert len(rows)==2 and not m['errors']
    assert calls.count('/miui/sample/stable/A/')==1


def test_batch_pending_pages_remain_pending_and_conflicts_block_old_candidates(env,monkeypatch):
    from test_vendor_catalogue import clients,finish
    db,_=env;count=33
    def route(req):
        if req.url.host=='api.github.com':return httpx.Response(204)
        if req.url.host=='raw.githubusercontent.com':return httpx.Response(200,headers={'Content-Type':'text/plain'},text='sample: [Sample]\n')
        if req.url.path.startswith('/archive/miui/'):
            return httpx.Response(200,headers={'Content-Type':'text/html'},text=''.join(f'<a href="/miui/sample/stable/V{i:02}/">v</a>' for i in range(count)))
        if req.url.path.startswith('/archive/hyperos/'):return httpx.Response(404)
        ver='V'+req.url.path.split('/')[-2][1:]+'.0.0.0.TXXMIXM'
        return httpx.Response(200,headers={'Content-Type':'text/html'},text=detail(version=ver))
    clients(monkeypatch,route)
    assert job.run(db,'xiaomi')==0
    store=Store(db,'xiaomi');_,model=store.rows('model')[0]
    assert model['status']=='pending' and model['error']=='HistoryPagesPending' and model['attempts']==0
    assert store.get(store.key('control'))['state']=='working'
    finish(db,store.get(store.key('control'))['cycle'],'xiaomi')
    assert store.counts()['models']=={'ok':1}
    assert store.counts()['releases']=={'resolved':33}


def test_source_conflict_blocks_previously_ready_local_candidate(env,monkeypatch):
    db,_=env;store=Store(db,'xiaomi')
    device={'vendor':'xiaomi','upstream_id':'sample','codename':'sample','name':'Sample'}
    release={'version':'V1','region':'CN','kind':'full_ota','download_url':'https://bigota.d.miui.com/V1/original.zip'}
    job.seed_models(store,[device],{});job.seed_releases(store,device,[release])
    key,value=store.rows('release')[0];value['status']='resolved';store.put(key,value)
    def collect(*args,**kwargs):return device,[],{'conflicts':[{'blocking':True,'key':['sample','CN','V1','recovery_zip'],'selected':release,'alternate':{**release,'download_url':'other'}}]}
    monkeypatch.setattr(job,'collect_model',collect)
    class Client:
        deadline=time.monotonic()+10;stats={}
    class HTTP:client=Client()
    class Status:
        def log(self,*args):pass
        def update(self,*args,**kwargs):pass
    more,counts,_=job.batch(store,{'cycle':'test','mode':'audit','operation':'incremental'},HTTP(),Status())
    assert not more and counts['models']=={'blocked':1}
    candidate=store.get(key)
    assert candidate['status']=='blocked' and candidate['error']=='SourceIdentityConflict'
    assert candidate['release']==release and not candidate['published']


def test_conflicting_same_url_checksums_do_not_overwrite_known_digest():
    def run(html):
        def route(req):
            if req.url.path.startswith('/archive/miui/'):return httpx.Response(200,text='<a href="/miui/sample/stable/V1/">v</a>')
            if req.url.path.startswith('/archive/hyperos/'):return httpx.Response(404)
            return httpx.Response(200,text=html)
        with httpx.Client(transport=httpx.MockTransport(route)) as raw:return discover_device('sample',raw)
    rows,m=run(detail()+detail().replace('a'*32,'Unknown'))
    assert len(rows)==1 and rows[0]['md5']=='a'*32 and not m['errors']
    rows,m=run(detail()+detail().replace('a'*32,'b'*32))
    assert not rows and m['conflicts'][0]['kind']=='checksum_conflict'
    assert m['conflicts'][0]['blocking']
