import json,hashlib
from copy import deepcopy
import pytest
from test_vendor_catalogue import env
from test_history_sweep import detail
from fmr_ingest_stock.history import parse_detail,conflict_kind,discover_device,HistoryCheckpoint,unavailable_reason
from fmr_ingest_stock.xiaomi_variants import annotate_platforms,android_version
from fmr_ingest_stock.catalogue_store import Store
from fmr_ingest_stock.catalogue_sync import publish_release,publish_unavailable
from fmr_ingest_stock.catalogue_job import xiaomi_release
from fmr_ingest_stock import vendor_catalogue as job
from fmr_ingest_stock.xiaomi_identity import reconcile_cached_sources,repair
import httpx

def device(code):
    return {'vendor':'xiaomi','upstream_id':code,'codename':code,'name':code}

def head(monkeypatch,tmp_path,url,status=404):
    monkeypatch.setenv('FMR_CATALOGUE_STATE_DIR',str(tmp_path))
    (tmp_path/'xiaomi-source-heads.json').write_text(json.dumps({hashlib.sha256(url.encode()).hexdigest():{'checked_at':1,'head':{'status':status,'original_status':403,'length':'9000000' if status==200 else '475','type':'application/zip' if status==200 else 'application/xml'}}}))

@pytest.mark.parametrize('code,version,names,expected',[
 ('mido','7.2.16',['miui_HMNote4XGlobal_7.2.16_8158e4d139_7.0.zip','miui_HMNote4XGlobal_7.2.16_e6eed016d8_6.0.zip','mido_global_images_7.2.16_20170216.0000.00_6.0_global_aaaaaaaaaa.tgz'],['7.0','6.0','6.0']),
 ('vince','8.4.24',['miui_HM5PlusGlobal_8.4.24_6b42bb9d12_7.1.zip','miui_HM5PlusGlobal_8.4.24_cbb48d89b4_8.1.zip','vince_global_images_8.4.24_20180424.0000.00_7.1_global_aaaaaaaaaa.tgz'],['7.1','8.1','7.1'])])
def test_cross_android_weeklies_split_without_losing_matching_package_pairs(env,code,version,names,expected):
    db,_=env;store=Store(db,'xiaomi')
    html=''.join(detail(code=code,version=version,kind='Recovery' if n.endswith('.zip') else 'Fastboot',filename=n) for n in names)
    rows,_=parse_detail(html,code);annotate_platforms(rows)
    assert [r['android'] for r in rows]==expected
    assert all(r['variant_id']=='android-'+v.replace('.','-') for r,v in zip(rows,expected))
    releases=[xiaomi_release(device(code),r) for r in rows]
    job.seed_releases(store,device(code),releases)
    results=[publish_release(db,device(code),r) for r in releases]
    assert len({r['public_token'] for r in results})==3
    assert len(db.query('SELECT * FROM stock_firmwares'))==3
    assert {json.loads(r['metadata_json'])['display_version'] for r in db.query('SELECT metadata_json FROM firmware_metadata')}=={version}

@pytest.mark.parametrize('name,want',[
 ('miui_X_1_aaaaaaaaaa_4.4.zip','4.4'),('miui_X_1_aaaaaaaaaa_5.1.zip','5.1'),
 ('vince_global_images_8.4.24_20180424.0000.00_7.1_global_aaaaaaaaaa.tgz','7.1'),
 ('popsicle-images-OS4.0.5.0.XPBCNXM-user-16.0-aabbccddee.tgz','16.0')])
def test_android_minor_versions_are_preserved(name,want):assert android_version(name)==want

@pytest.mark.parametrize('a,b',[
 ('chopin_id_global_images_V12.5.1.0.RKPIDXM_20210629.0000.00_11.0_global_1a4fca9831.tgz','chopin_id_global_images_V12.5.1.0.RKPIDXM_20210629.0000.00_11.0_global_1b774ec1eb.tgz'),
 ('miui_CHOPINIDGlobal_V12.5.1.0.RKPIDXM_3898f2fc02_11.0.zip','miui_CHOPINIDGlobal_V12.5.1.0.RKPIDXM_71ee16c618_11.0.zip'),
 ('equuleus_global_images_9.1.17_20190117.0000.00_9.0_global_29e92cd595.tgz','equuleus_global_images_9.1.17_20190117.0000.00_9.0_global_dec766277d.tgz'),
 ('gemini_images_V7.5.4.0.MAACNDE_20160523.0000.23_6.0_cn_f93a250f2d.tgz','gemini_images_V7.5.4.0.MAACNDE_20160523.0000.23_6.0_cn_16b925e3d1.tgz')])
def test_same_build_repack_preserves_alternates_even_without_md5(a,b):
    assert conflict_kind({'link':'https://bigota.d.miui.com/'+a},{'link':'https://bigota.d.miui.com/'+b})==('repacked_archive',False)
    assert conflict_kind({'link':'https://bigota.d.miui.com/'+a,'md5':'0'*32},{'link':'https://bigota.d.miui.com/'+b})[1]

def test_patch_version_and_unavailable_archive_do_not_occupy_the_working_version(env,monkeypatch,tmp_path):
    db,_=env;d=device('whyred');name='whyred_global_images_8.12.6.1_20181206.0000.00_8.1_global_e4b4cc8404.tgz'
    rows,_=parse_detail(detail(code='whyred',version='8.12.6',kind='Fastboot',filename=name),'whyred')
    assert rows[0]['version']=='8.12.6.1' and rows[0]['source_version']=='8.12.6'
    bad=xiaomi_release(d,rows[0])
    # Simulate a legacy record indexed by the source header.
    legacy={**bad,'version':'8.12.6'};legacy.pop('source_version',None)
    from fmr_core.crypto import generate_public_token,sign_firmware
    from fmr_ingest_stock.catalogue_sync import publish_device
    from fmr_ingest_stock.cdn_rewriter import rewrite_to_fast_cdn
    from fmr_ingest_stock.common import private_key
    publish_device(db,d);url=bad['download_url'];fast=rewrite_to_fast_cdn('xiaomi',url);token=generate_public_token('fw')
    db.execute("INSERT INTO stock_firmwares(public_token,device_codename,region_code,model_code,version_str,android_version,package_type,original_official_url,fast_cdn_url,ed25519_signature,extract_status) VALUES (?,?,?,?,?,?,?,?,?,?,?)",(token,'whyred','Global','whyred_global','8.12.6','8.1','fastboot_tgz',url,fast,sign_firmware(token,fast,private_key()),'skipped'))
    before=db.query('SELECT * FROM stock_firmwares')[0]
    head(monkeypatch,tmp_path,url)
    assert unavailable_reason(bad)=='DownloadMirrorUnavailable'
    publish_unavailable(db,d,bad,'DownloadMirrorUnavailable')
    meta=json.loads(db.query('SELECT metadata_json FROM firmware_metadata WHERE public_token=?',(token,))[0]['metadata_json'])
    assert meta['availability']=='unavailable' and meta['display_version']=='8.12.6.1'
    good={**legacy,'download_url':url.replace('8.12.6.1_','8.12.6_').replace('e4b4cc8404','3504c62e6e')}
    out=publish_release(db,d,good)
    assert out['public_token']!=token
    after=db.query('SELECT * FROM stock_firmwares WHERE public_token=?',(token,))[0]
    for k in ('public_token','original_official_url','fast_cdn_url','ed25519_signature','version_str','model_code'):assert after[k]==before[k]
    assert len(db.query('SELECT * FROM stock_firmwares'))==2

def test_unavailable_without_an_existing_published_file_does_not_invent_a_download(env):
    db,_=env
    r={'version':'V1','region':'CN','kind':'full_ota','download_url':'https://bigota.d.miui.com/V1/miui_SAMPLE_V1_aaaaaaaaaa_14.0.zip'}
    assert publish_unavailable(db,device('sample'),r,'UpstreamNotFullROM')['status']=='unavailable_recorded'
    assert not db.query('SELECT * FROM stock_firmwares')

def test_legacy_platform_file_reuses_original_token_and_signature(env):
    db,_=env;d=device('mido')
    old={'version':'7.2.16','region':'Global','kind':'full_ota','download_url':'https://bigota.d.miui.com/7.2.16/miui_HMNote4XGlobal_7.2.16_e6eed016d8_6.0.zip'}
    token=publish_release(db,d,old)['public_token'];before=db.query('SELECT * FROM stock_firmwares')[0]
    updated={**old,'variant_id':'android-6-0','variant_label':'Android 6.0 archive'}
    assert publish_release(db,d,updated)['public_token']==token
    after=db.query('SELECT * FROM stock_firmwares')[0]
    assert after==before
    metadata=json.loads(db.query('SELECT metadata_json FROM firmware_metadata')[0]['metadata_json'])
    assert metadata['variant_label']=='Android 6.0 archive'

def test_cached_android_conflict_repair_retires_only_exact_legacy_file_atomically(env):
    db,_=env;d=device('mido');store=Store(db,'xiaomi')
    names=['miui_HMNote4XGlobal_7.2.16_8158e4d139_7.0.zip','miui_HMNote4XGlobal_7.2.16_e6eed016d8_6.0.zip']
    rows,_=parse_detail(''.join(detail(code='mido',version='7.2.16',filename=n) for n in names),'mido')
    # detail() defaults to one synthetic MD5, irrelevant to distinct platform identities.
    for r in rows:r['md5']=None
    job.seed_releases(store,d,[xiaomi_release(d,rows[0])])
    key,value=store.rows('release')[0];value.update(status='blocked',error='SourceIdentityConflict',published=True,publication={'public_token':'receipt'});store.put(key,value)
    url='https://xmfirmwareupdater.com/miui/mido/weekly/7.2.16/'
    HistoryCheckpoint(store).save('mido',url,{'rows':rows,'url':url,'sha256':'proof'})
    class Status:
        def log(self,*args):pass
    assert reconcile_cached_sources(store,Status())==1
    assert store.get(key)['status']=='superseded'
    active=[v for _,v in store.rows('release') if v['status']!='superseded']
    assert len(active)==2 and {v['release']['variant_id'] for v in active}=={'android-6-0','android-7-0'}
    assert not any(v.get('published') for v in active)
    assert store.get(key)['publication']=={'public_token':'receipt'}
