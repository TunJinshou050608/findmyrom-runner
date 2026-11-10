"""Shared family ownership, exact OEM tenant validation and immutable source moves."""
import json
from copy import deepcopy
import httpx
import pytest
from test_vendor_catalogue import env
from test_history_sweep import detail
from fmr_ingest_stock.history import parse_detail,discover_device,HistoryCheckpoint,official
from fmr_ingest_stock.catalogue_store import Store
from fmr_ingest_stock.oem_sources import xiaomi_devices,download_origin
from fmr_ingest_stock import vendor_catalogue as job
from fmr_ingest_stock.xiaomi_identity import repair
from fmr_ingest_stock.xiaomi_devices import base_code
from fmr_ingest_stock.catalogue_sync import publish_release,FirmwareIdentityPending
from fmr_ingest_stock.cdn_rewriter import FAST_HOST

@pytest.mark.parametrize('family,owner,version,names',[
 ('emerald','emeraldr','OS3.0.306.0.WFOMIXM',[
  'emerald_r_global-ota_full-OS3.0.306.0.WFOMIXM-user-16.0-2c23fa0a2a.zip',
  'emerald_r_global_images_OS3.0.306.0.WFOMIXM_20260922.0000.00_16.0_global_65f2532a24.tgz']),
 ('shennong','shennongt','OS1.0.7.0.UNDCNXM',[
  'miui_SHENNONGT_OS1.0.7.0.UNDCNXM_4b29ecc269_14.0.zip',
  'shennong_t_images_OS1.0.7.0.UNDCNXM_20240801.0000.00_14.0_cn_15a5d94034.tgz'])])
def test_shared_archive_filters_physical_ownership_and_reuses_family_checkpoints(env,family,owner,version,names):
    db,_=env;store=Store(db,'xiaomi');calls=[]
    special=''.join(detail(version=version,code=family,kind='Recovery' if n.endswith('.zip') else 'Fastboot',filename=n) for n in names)
    normal=detail(version='OS1.0.1.0.UNBCNXM',code=family,filename='miui_'+family.upper()+'_OS1.0.1.0.UNBCNXM_aaaaaaaaaa_14.0.zip')
    parsed,_=parse_detail(special,family)
    assert len(parsed)==2 and all(base_code(r['codename'])==owner for r in parsed)
    def route(req):
        calls.append(req.url.path)
        if req.url.path.startswith('/archive/miui/'):return httpx.Response(404)
        if req.url.path.startswith('/archive/hyperos/'):return httpx.Response(200,text=f'<a href="/hyperos/{family}/stable/V1/">v</a>')
        assert req.url.path==f'/hyperos/{family}/stable/V1/'
        return httpx.Response(200,text=special+normal)
    with httpx.Client(transport=httpx.MockTransport(route)) as client:
        rows,manifest=discover_device(owner,client,checkpoint=HistoryCheckpoint(store),max_workers=1)
        assert len(rows)==2 and not manifest['errors']
        rows,manifest=discover_device(family,client,checkpoint=HistoryCheckpoint(store),max_workers=1)
        assert len(rows)==1 and not manifest['errors']
    assert calls.count(f'/hyperos/{family}/stable/V1/')==1
    assert not any('/archive/hyperos/'+owner+'/'==p for p in calls)

def test_device_dictionary_preserves_underscored_products_without_merging_families():
    class HTTP:
        def get(self,url):
            return b'emeraldr: [POCO M6 Pro Global]\nemerald_r_global: [Redmi Note 14S Global]\nemerald_global: [Redmi Note 13 Pro Global]\nshennong: [Xiaomi 14 Pro]\nshennong_t: [Xiaomi 14 Pro Ti Satellite]\nshennongt: [Xiaomi 14 Pro Ti Satellite]\n'
    devices,errors=xiaomi_devices(HTTP());by={d['upstream_id']:d for d in devices}
    assert set(by)=={'emerald','emeraldr','shennong','shennongt'} and not errors
    assert by['emeraldr']['name']=='Redmi Note 14S'
    assert by['emeraldr']['route_code']=='emerald'
    assert by['shennongt']['route_code']=='shennong'
    assert 'Redmi Note 14S' not in by['emerald']['aliases']

def test_exact_existing_xiaomi_storage_tenant_passes_without_trusting_other_tenants():
    html=detail().replace('bigota.d.miui.com',FAST_HOST)
    assert official('https://'+FAST_HOST+'/rom.zip')
    assert download_origin('xiaomi','https://'+FAST_HOST+'/rom.zip')=='oem'
    assert len(parse_detail(html,'sample')[0])==1
    for host in ['attacker.oss-ap-southeast-1.aliyuncs.com',FAST_HOST+'.evil.example']:
        assert not official('https://'+host+'/rom.zip')
        assert not download_origin('xiaomi','https://'+host+'/rom.zip')
        assert not parse_detail(html.replace(FAST_HOST,host),'sample')[0]

def test_legacy_wrong_product_is_moved_with_original_receipt_preserved(env):
    db,_=env;store=Store(db,'xiaomi')
    device={'vendor':'xiaomi','upstream_id':'emerald','codename':'emerald','name':'Redmi Note 13 Pro'}
    release={'version':'OS3.0.306.0.WFOMIXM','region':'Global','kind':'full_ota',
             'download_url':'https://bigota.d.miui.com/OS3.0.306.0.WFOMIXM/emerald_r_global-ota_full-OS3.0.306.0.WFOMIXM-user-16.0-2c23fa0a2a.zip'}
    job.seed_releases(store,device,[release])
    key,value=store.rows('release')[0];value.update(status='resolved',published=True,publication={'public_token':'original'})
    store.put(key,value);original=deepcopy(value)
    class Status:
        def log(self,*args):pass
    while repair(store,Status(),limit=1):pass
    active=[v for _,v in store.rows('release') if v['status']!='superseded']
    assert len(active)==1 and active[0]['device']['upstream_id']=='emeraldr'
    assert active[0]['status']=='pending' and not active[0]['published']
    evidence=[v['record'] for _,v in store.rows('identity_evidence')]
    assert any(v['release']==original['release'] and v['publication']==original['publication'] for v in evidence)
    with pytest.raises(FirmwareIdentityPending):publish_release(db,device,release)
    assert not db.query('SELECT * FROM stock_firmwares')


def test_conflict_audit_is_read_only_uses_only_head_and_never_logs_secret_fields(env,monkeypatch,capsys):
    from pathlib import Path
    from fmr_ingest_stock import xiaomi_source_audit as audit
    db,path=env;root=Path(path).parent;target=root/'xiaomi.sqlite'
    import sqlite3
    device={'vendor':'xiaomi','upstream_id':'sample','codename':'sample','name':'Sample'}
    release={'version':'V1','region':'CN','kind':'full_ota','download_url':'https://bigota.d.miui.com/V1/sample.zip'}
    store=Store(db,'xiaomi');job.seed_releases(store,device,[release]);key,value=store.rows('release')[0]
    value.update(status='blocked',error='LegacyIdentityConflict',publication={'public_token':'PRIVATE_RECEIPT'})
    store.put(key,value)
    with sqlite3.connect(path) as source:
        with sqlite3.connect(target) as destination:source.backup(destination)
    before=target.read_bytes()
    monkeypatch.setenv('FMR_CATALOGUE_STATE_DIR',str(root));seen=[]
    class Client:
        def __init__(self,**kwargs):assert kwargs['follow_redirects'] is False
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def head(self,url):
            seen.append(url)
            return httpx.Response(404,headers={'content-length':'464','content-type':'text/html'})
    monkeypatch.setattr(audit.httpx,'Client',Client)
    assert audit.main(['--network'])==0
    output=capsys.readouterr().out
    assert seen==[release['download_url']]
    assert 'PRIVATE_RECEIPT' not in output and 'https://' not in output
    assert 'sample.zip' in output and '"status": 404' in output
    assert target.read_bytes()==before


def test_archive_transport_gap_uses_short_backoff_and_keeps_successful_history():
    import time
    def route(req):
        if req.url.path.startswith('/archive/miui/'):return httpx.Response(503)
        if req.url.path.startswith('/archive/hyperos/'):return httpx.Response(200,text='<a href="/hyperos/sample/stable/V1/">v</a>')
        return httpx.Response(200,text=detail())
    with httpx.Client(transport=httpx.MockTransport(route)) as client:
        before=time.time();rows,manifest=discover_device('sample',client,max_workers=1)
    assert len(rows)==1
    assert manifest['errors'][0]['status']==503
    assert before+899<=manifest['retry_after']<=time.time()+900


def test_partially_untrusted_full_package_is_reported_as_incomplete():
    html=detail()+detail(kind='Fastboot').replace('bigota.d.miui.com','attacker.example')
    def route(req):
        if req.url.path.startswith('/archive/miui/'):return httpx.Response(404)
        if req.url.path.startswith('/archive/hyperos/'):return httpx.Response(200,text='<a href="/hyperos/sample/stable/V1/">v</a>')
        return httpx.Response(200,text=html)
    with httpx.Client(transport=httpx.MockTransport(route)) as client:
        rows,manifest=discover_device('sample',client,max_workers=1)
    assert len(rows)==1 and manifest['rejected_records']==1
    assert manifest['errors'][0]['error']=='RejectedFullROMRecord'
