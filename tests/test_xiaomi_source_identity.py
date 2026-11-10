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
    assert by['emeraldr']['aliases']==['Redmi Note 14S']
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
            return httpx.Response(403 if 'aliyuncs.com' not in url else 200,headers={'content-length':'464','content-type':'text/html'})
    monkeypatch.setattr(audit.httpx,'Client',Client)
    assert audit.main(['--network'])==0
    output=capsys.readouterr().out
    from fmr_ingest_stock.cdn_rewriter import rewrite_to_fast_cdn
    assert seen==[release['download_url'],rewrite_to_fast_cdn('xiaomi',release['download_url'])]
    assert 'PRIVATE_RECEIPT' not in output and 'https://' not in output
    assert 'sample.zip' in output and '"status": 200' in output
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


def test_attested_umi_repack_preserves_existing_archive_and_signature(env):
    from fmr_ingest_stock.history import conflict_kind
    db,_=env;version='V12.2.2.0.RJBCNXM'
    names=['umi_images_'+version+'_20201105.0000.00_11.0_cn_'+h*10+'.tgz' for h in ('a','b')]
    a={'link':'https://bigota.d.miui.com/'+version+'/'+names[0],'md5':'a'*32,'size':'4.8 GB'}
    b={'link':'https://bigota.d.miui.com/'+version+'/'+names[1],'md5':'b'*32,'size':'4.8 GB'}
    assert conflict_kind(a,b)==('repacked_archive',False)
    assert conflict_kind(a,{**b,'md5':'c'*32})==('package_variant',True)
    device={'vendor':'xiaomi','upstream_id':'umi','codename':'umi','name':'Mi 10'}
    release={'version':version,'region':'CN','kind':'fastboot','download_url':b['link'],'md5':b['md5']}
    first=publish_release(db,device,release)
    original=db.query('SELECT * FROM stock_firmwares')
    result=publish_release(db,device,{**release,'download_url':a['link'],'md5':a['md5'],'archive_revisions':[b]})
    assert result['public_token']==first['public_token']
    immutable=('public_token','device_codename','region_code','model_code','version_str','package_type','original_official_url','fast_cdn_url','md5_or_sha256','ed25519_signature','extract_status')
    current=db.query('SELECT * FROM stock_firmwares')
    assert [{k:r[k] for k in immutable} for r in current]==[{k:r[k] for k in immutable} for r in original]
    assert current[0]['file_size_str']=='4.8 GB'
    metadata=json.loads(db.query('SELECT metadata_json FROM firmware_metadata')[0]['metadata_json'])
    assert metadata['original_filename']==names[1]


def test_source_conflict_cannot_age_into_ready_without_a_clean_source_result(env):
    db,_=env;store=Store(db,'xiaomi')
    device={'vendor':'xiaomi','upstream_id':'sample','codename':'sample','name':'Sample'}
    release={'version':'V1','region':'CN','kind':'full_ota','download_url':'https://bigota.d.miui.com/V1/sample.zip'}
    job.seed_releases(store,device,[release]);key,value=store.rows('release')[0]
    value.update(status='blocked',error='SourceIdentityConflict',next_check=0,last_cycle='',published=True,publication={'public_token':'receipt'})
    store.put(key,value)
    assert not store.due('release','next',10,operation='retry_failed')
    job.seed_releases(store,device,[release])
    recovered=store.get(key)
    assert recovered['status']=='pending' and recovered['error'] is None
    assert recovered['publication']==value['publication'] and recovered['published']
    assert store.due('release','next',10)


def test_missing_source_error_documents_are_retained_as_unavailable_not_roms():
    bad=detail(version='OS1.0.4.0.UNKCNXM',code='vermeer').replace('6.0 GB','464 Bytes').replace('2023-01-01','None').replace('a'*32,'Unknown')
    good=detail(version='OS1.0.5.0.UNKCNXM',code='vermeer')
    def route(req):
        if req.url.path.startswith('/archive/miui/'):return httpx.Response(404)
        if req.url.path.startswith('/archive/hyperos/'):return httpx.Response(200,text='<a href="/hyperos/vermeer/stable/V1/">v</a>')
        return httpx.Response(200,text=bad+good)
    with httpx.Client(transport=httpx.MockTransport(route)) as client:
        rows,manifest=discover_device('vermeer',client,max_workers=1)
    assert len(rows)==1 and rows[0]['version']=='OS1.0.5.0.UNKCNXM'
    assert manifest['unavailable_records'][0]['version']=='OS1.0.4.0.UNKCNXM'
    assert manifest['errors'][0]['error']=='UpstreamNotFullROM'


def test_legacy_repack_conflict_reconciles_locally_without_erasing_either_receipt(env):
    from fmr_ingest_stock.xiaomi_identity import reconcile_conflict
    db,_=env;store=Store(db,'xiaomi');version='V12.2.2.0.RJBCNXM'
    device={'vendor':'xiaomi','upstream_id':'umi','codename':'umi','name':'Mi 10'}
    def row(h):return {'version':version,'region':'CN','kind':'fastboot','md5':h*32,'size':'4.8 GB',
       'download_url':'https://bigota.d.miui.com/'+version+'/umi_images_'+version+'_20201105.0000.00_11.0_cn_'+h*10+'.tgz'}
    job.seed_releases(store,device,[row('a')]);key,value=store.rows('release')[0]
    evidence=store.key('identity_evidence','alternate')
    alternate={'device':device,'release':row('b'),'published':True,'publication':{'public_token':'receipt_b'}}
    store.put(evidence,{'record':alternate})
    value.update(status='blocked',error='LegacyIdentityConflict',identity_review_sources=[evidence],
                 published=True,publication={'public_token':'receipt_a'})
    store.put(key,value)
    assert reconcile_conflict(store,key,value)
    resolved=store.get(key)
    assert resolved['status']=='pending' and resolved['error'] is None
    assert resolved['publication']==value['publication'] and resolved['published']
    assert resolved['release']['archive_revisions'][0]['md5']=='b'*32
    assert store.get(evidence)['record']==alternate
    assert store.rows('identity_resolution_evidence')[0][1]['record']==value
    assert not reconcile_conflict(store,key,resolved)


def test_snapshot_audit_fails_on_wrong_identity_without_touching_the_database(env,monkeypatch,capsys):
    from pathlib import Path
    import sqlite3
    from fmr_ingest_stock import xiaomi_source_audit as audit
    db,path=env;root=Path(path).parent;target=root/'xiaomi.sqlite';store=Store(db,'xiaomi')
    device={'vendor':'xiaomi','upstream_id':'sample','codename':'sample','name':'Sample'}
    row={'version':'V1','region':'CN','kind':'full_ota','download_url':'https://bigota.d.miui.com/V1/sample.zip'}
    store.put(store.key('release','wrong'),{'device':device,'release':row,'status':'pending','published':False})
    with sqlite3.connect(path) as source:
        with sqlite3.connect(target) as destination:source.backup(destination)
    before=target.read_bytes();monkeypatch.setenv('FMR_CATALOGUE_STATE_DIR',str(root))
    assert audit.main([])==1
    output=capsys.readouterr().out
    assert '"active_identity_mismatches": 1' in output
    assert '"counts_consistent": true' in output
    assert target.read_bytes()==before


@pytest.mark.parametrize('code,variant,names',[
 ('hermes','cmonly',['miui_HMNote2CM_BUILD_hash_5.0.zip','hermes_cmonly_images_BUILD_stamp_5.0_cn_hash.tgz']),
 ('virgo','lte-ct',['miui_MINoteCT_BUILD_hash_4.4.zip','virgo_lte_ct_images_BUILD_stamp_4.4_cn_hash.tgz']),
 ('wt86047','pro',['miui_HM2XTDPro_BUILD_hash_5.1.zip','wt86047_pro_images_BUILD_stamp_5.1_cn_hash.tgz']),
 ('wt88047','pro',['miui_HM2XWCProGlobal_BUILD_hash_5.1.zip','wt88047_pro_global_images_BUILD_stamp_5.1_global_hash.tgz']),
 ('picasso','48mp',['miui_PICASSO48MPRE_BUILD_hash_12.0.zip','picasso_48m_images_BUILD_stamp_12.0_cn_hash.tgz']),
 ('lcsh92','td-scdma',['miui_HMNoteTD_BUILD_hash_4.4.zip','lcsh92_wet_xm_td_images_BUILD_stamp_4.4_cn_hash.tgz']),
 ('dandelion','c3l2',['miui_DANDELIONC3L2EEAGlobal_BUILD_hash_11.0.zip','dandelion_c3l2_eea_global_images_BUILD_stamp_11.0_eea_hash.tgz']),
 ('rosemary','poco-m5s',['miui_ROSEMARYPGlobal_BUILD_hash_13.0.zip','rosemary_p_global_images_BUILD_stamp_13.0_global_hash.tgz']),
 ('sweet','k6a',['miui_SWEETK6AGlobal_BUILD_hash_13.0.zip','sweet_k6a_global_images_BUILD_stamp_13.0_global_hash.tgz']),
 ('curtana','rf',['miui_CURTANAINRFGlobal_BUILD_hash_12.0.zip','curtana_in_rf_global_images_BUILD_stamp_12.0_in_hash.tgz']),
 ('angelicain','rf',['miui_ANGELICAININRFGlobal_BUILD_hash_11.0.zip','angelicain_in_rf_global_images_BUILD_stamp_11.0_in_hash.tgz']),
 ('pissarro','fk',['miui_PISSARROINFKGlobal_BUILD_hash_13.0.zip','pissarro_in_fk_global_images_BUILD_stamp_13.0_in_hash.tgz'])])
def test_real_upstream_editions_cover_both_recovery_and_fastboot(code,variant,names):
    from fmr_ingest_stock.xiaomi_variants import edition,LABELS
    from fmr_ingest_stock.firmware_sync import normalize
    for name in names:
        assert edition(code,name)==variant
        row={'codename':code,'version':'V1.0.0.0.TESTCNXM','link':'https://bigota.d.miui.com/V1/'+name,'variant_id':variant}
        assert normalize(row,'xiaomi')['model_code']==code+'__'+variant
        assert LABELS[variant]


@pytest.mark.parametrize('code,name',[
 ('sakura','miui_HM6ProINGlobal_V12.0.4.0.PDMMIXM_hash_9.0.zip'),
 ('sakura','sakura_india_global_images_V12.0.4.0.PDMMIXM_stamp_9.0_global_hash.tgz'),
 ('curtana','miui_CURTANAINRFGlobal_V14.0.6.0.SJWINRF_hash_12.0.zip'),
 ('angelicain','angelicain_in_rf_global_images_V12.5.5.0.RCRINRF_stamp_11.0_in_hash.tgz'),
 ('pissarro','miui_PISSARROINFKGlobal_OS1.0.13.0.TKTINFK_hash_13.0.zip')])
def test_legacy_india_markets_do_not_default_to_global(code,name):
    from fmr_ingest_stock.history import model_code
    from fmr_ingest_stock.firmware_sync import region_code
    assert region_code(model_code(code,'V12.0.4.0.PDMMIXM',name),'V12.0.4.0.PDMMIXM')=='IN'


@pytest.mark.parametrize('selected_pro',[False,True])
def test_cached_source_reconciliation_recovers_both_hardware_packages_without_network(env,selected_pro,monkeypatch):
    from fmr_ingest_stock.xiaomi_identity import reconcile_cached_sources
    from fmr_ingest_stock.catalogue_job import xiaomi_release
    from fmr_ingest_stock.history import model_code
    db,_=env;store=Store(db,'xiaomi');version='7.11.16'
    device={'vendor':'xiaomi','upstream_id':'wt86047','codename':'wt86047','name':'Redmi 2'}
    names=['miui_HM2XTD_'+version+'_aaaaaaaaaa_4.4.zip','miui_HM2XTDPro_'+version+'_bbbbbbbbbb_5.1.zip']
    raw=[]
    for name,h in zip(names,('a','b')):
        rows,_=parse_detail(detail(version=version,code='wt86047',filename=name).replace('a'*32,h*32),'wt86047')
        raw.extend(rows)
    selected=next(r for r in raw if ('Pro' in r['link'])==selected_pro)
    release=xiaomi_release(device,selected)
    job.seed_models(store,[device],{});job.seed_releases(store,device,[release])
    key,value=store.rows('release')[0]
    value.update(status='blocked',error='SourceIdentityConflict',published=True,publication={'public_token':'receipt'})
    store.put(key,value)
    url='https://xmfirmwareupdater.com/miui/wt86047/weekly/'+version+'/'
    HistoryCheckpoint(store).save('wt86047',url,{'revision':2,'url':url,'sha256':'source_digest','rows':raw,'error':None})
    class Status:
        def log(self,*args):pass
    before=deepcopy(value);original_rows=dict(store.rows('release'));execute=db.execute_batch
    def fail(statements):return execute([*statements,('INSERT INTO nonexistent_table VALUES (1)',())])
    monkeypatch.setattr(db,'execute_batch',fail)
    with pytest.raises(Exception):reconcile_cached_sources(store,Status())
    assert dict(store.rows('release'))==original_rows and not store.rows('identity_resolution_evidence')
    monkeypatch.setattr(db,'execute_batch',execute)
    assert reconcile_cached_sources(store,Status())==1
    active=[v for _,v in store.rows('release') if v['status']!='superseded']
    assert len(active)==2 and {v['release'].get('variant_id') for v in active}=={None,'pro'}
    assert all(v['status']=='pending' for v in active)
    if not selected_pro:assert store.get(key)['publication']==before['publication'] and store.get(key)['published']
    else:assert not any(v['published'] for v in active)
    assert any(v['record']==before for _,v in store.rows('identity_resolution_evidence'))
    assert reconcile_cached_sources(store,Status())==0
    assert not db.query('SELECT * FROM stock_firmwares')


def test_changed_quarantined_source_keeps_original_publication_receipt(env):
    db,_=env;store=Store(db,'xiaomi')
    device={'vendor':'xiaomi','upstream_id':'sample','codename':'sample','name':'Sample'}
    release={'version':'V1','region':'CN','kind':'full_ota','download_url':'https://bigota.d.miui.com/V1/sample.zip'}
    job.seed_releases(store,device,[release]);key,value=store.rows('release')[0]
    value.update(status='blocked',error='SourceIdentityConflict',published=True,publication={'public_token':'original'})
    store.put(key,value)
    assert job.seed_releases(store,device,[{**release,'size':'2.0 GB'}])==1
    assert store.get(key)['status']=='pending'
    assert store.rows('release_revision_evidence')[0][1]['record']==value
