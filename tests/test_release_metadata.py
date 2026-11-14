import json
from copy import deepcopy
import pytest
from fmr_ingest_stock.release_metadata import normalize,REVISION
from fmr_ingest_stock import catalogue_publish as publisher
from fmr_ingest_stock.catalogue_sync import publish_release
from fmr_ingest_stock.vendor_catalogue import seed_releases,input_hash
from fmr_ingest_stock.catalogue_store import Store
from fmr_core.firmware_metadata_schema import ensure
from test_vendor_catalogue import env


@pytest.mark.parametrize('revision',[2,3])
def test_publisher_preserves_both_revision_acknowledgements_for_unchanged_release(env,revision):
    from fmr_ingest_stock.catalogue_store import digest
    db, _ = env
    device, release = source()
    store = Store(db, 'oneplus')
    seed_releases(store, device, [release])
    key, value = store.rows('release')[0]
    value['status'] = 'resolved'
    store.put(key, value)
    ledger = publisher.open_ledger(':memory:')
    try:
        ledger.execute('INSERT OR REPLACE INTO published VALUES (?,?,?)',
                       ('oneplus', key, digest([device, release, revision])))
        assert list(publisher.candidates(db, ledger, 'oneplus')) == []
        value['release'] = {**release, 'size': '2 GB'}
        store.put(key, value)
        assert len(list(publisher.candidates(db, ledger, 'oneplus'))) == 1
    finally:
        ledger.close()


def source():
    return {'vendor':'oneplus','upstream_id':'sample','codename':'sample','name':'Sample','page':'https://fwatlas.com/oneplus/sample'}, {'version':'CPH1234_15.0.1(EX01)','region':'EU','android_version':'OxygenOS 15','kind':'full_ota','date':'2025-01-01','size':'1 GB','download_url':'https://ota.oneplus.net/sample.zip','origin_kind':'oem','resolution':'resolved'}


def test_collector_metadata_cache_changes_do_not_requeue_published_firmware(env):
    db, _ = env
    device, release = source()
    store = Store(db, 'oneplus')
    seed_releases(store, device, [release])
    key, value = store.rows('release')[0]
    value.update(status='resolved', metadata_dirty=True)
    value['release']['_metadata'] = {'display_version': 'old cached label'}
    store.put(key, value)
    ledger = publisher.open_ledger(':memory:')
    try:
        item, = publisher.candidates(db, ledger, 'oneplus')
        assert publisher.sync_one(db, ledger, 'oneplus', item)
        value['release']['_metadata'] = {'display_version': 'recalculated cache'}
        store.put(key, value)
        assert list(publisher.candidates(db, ledger, 'oneplus')) == []
        value['release']['date'] = '2025-01-02'
        store.put(key, value)
        assert len(list(publisher.candidates(db, ledger, 'oneplus'))) == 1
    finally:
        ledger.close()


def test_batch_reuses_device_and_stock_reads_without_skipping_metadata(env):
    db, _ = env
    device, release = source()
    db._publication_device_cache = {}
    publish_release(db, device, release)
    sql = []
    db._get_sqlite_conn().set_trace_callback(sql.append)
    result = publish_release(db, device, {**release, 'version': 'BUILD.2'})
    assert result['status'] == 'published_new' and result['metadata_changed']
    assert not any(q.lstrip().startswith('INSERT INTO devices') for q in sql)
    assert sum(q.startswith('SELECT') and 'FROM stock_firmwares' in q for q in sql) == 1
    sql.clear()
    result = publish_release(db, device, {**release, 'version': 'BUILD.2'})
    assert result['status'] == 'preserved_existing' and not result['metadata_changed']
    assert sum(q.startswith('SELECT') and 'FROM stock_firmwares' in q for q in sql) == 1
    assert not any(q.lstrip().startswith(('INSERT', 'UPDATE')) for q in sql)
    sql.clear()
    result = publish_release(db, device, {**release, 'version': 'BUILD.2', 'size': '2 GB'})
    assert result['metadata_changed']
    updates = [q for q in sql if q.startswith('UPDATE stock_firmwares')]
    assert len(updates) == 1 and 'SET file_size_str=' in updates[0]
    assert 'published_date' not in updates[0]


def test_metadata_updates_preserve_identity_signatures_and_retry_is_noop(env):
    db,_=env;device,release=source()
    result=publish_release(db,device,release)
    before=db.query('SELECT * FROM stock_firmwares')[0]
    assert before['android_version']=='unknown'
    metadata=json.loads(db.query('SELECT metadata_json FROM firmware_metadata')[0]['metadata_json'])
    assert metadata['os_name']=='OxygenOS' and metadata['os_version']=='15'
    assert metadata['display_version']==release['version']
    release={**release,'date':'2025-01-02','android_version':'Android 15'}
    result=publish_release(db,device,release)
    assert result['metadata_changed']
    after=db.query('SELECT * FROM stock_firmwares')[0]
    for key in before:
        if key not in ('android_version','published_date'):assert before[key]==after[key],key
    assert after['android_version']=='15' and after['published_date']=='2025-01-02'
    assert not publish_release(db,device,release)['metadata_changed']


def test_date_only_change_requeues_resolved_entry_without_losing_download(env):
    db,_=env;device,release=source();store=Store(db,'oneplus')
    seed_releases(store,device,[release]);key,value=store.rows('release')[0]
    value.update(status='resolved',published=True);store.put(key,value)
    changed={**release,'date':'2025-02-02'}
    assert input_hash(release)!=input_hash(changed)
    seed_releases(store,device,[changed])
    value=store.get(key)
    assert value['status']=='resolved' and value['metadata_dirty']
    assert value['release']['download_url']==release['download_url']
    ledger=publisher.open_ledger(':memory:')
    try:assert len(list(publisher.candidates(db,ledger,'oneplus')))==1
    finally:ledger.close()


def test_partial_batch_failure_does_not_acknowledge_and_retry_completes(env,monkeypatch):
    db,_=env;device,release=source();publish_release(db,device,release)
    before=db.query('SELECT * FROM firmware_metadata');release['date']='2025-03-03'
    original=db.execute_batch
    def fail(statements):return original([*statements,('INSERT INTO no_such_table VALUES (1)',())])
    monkeypatch.setattr(db,'execute_batch',fail)
    ledger=publisher.open_ledger(':memory:')
    item=('release','key',{'device':device,'release':release},'changed-hash')
    with pytest.raises(Exception):publisher.sync_one(db,ledger,'oneplus',item)
    assert db.query('SELECT * FROM firmware_metadata')==before
    assert not ledger.query('SELECT * FROM published')
    monkeypatch.setattr(db,'execute_batch',original)
    assert publisher.sync_one(db,ledger,'oneplus',item)
    assert ledger.query('SELECT hash FROM published')[0]['hash']=='changed-hash'
    ledger.close()


def test_metadata_contract_does_not_run_historical_migrations(env):
    db,_=env;ensure(db,apply=True)
    assert not db.query("SELECT name FROM sqlite_master WHERE name IN ('stock_firmwares_next','fmr_identity_maintenance')")


def test_samsung_metadata_is_scoped_and_complete_identity_stays_intact():
    d={'vendor':'samsung','upstream_id':'sm-a716u1','codename':'SM-A716U1'}
    r={'version':'A716U1UEU9GWF7/A716U1OYM9GWF7/A716U1UEU9GWF7/A716U1UEU9GWF7','region':'XAA','_official_metadata':{'model':'SM-A716U1','csc':'XAA','ap':'A716U1UEU9GWF7','android':'13','date':'2023-07-18','patch':'2023-06-01','source':'https://doc.samsungmobile.com/SM-A716U1/XAA/doc.html'}}
    original=deepcopy(r);m=normalize(d,r)
    assert m['display_version']=='A716U1UEU9GWF7' and m['android_version']=='13'
    assert m['security_patch']=='2023-06-01' and m['raw_version']==r['version']
    assert r==original
    assert normalize(d,{**r,'region':'VZW'})['android_version'] is None


def test_optional_samsung_metadata_failure_preserves_rows_and_caches_failure():
    from fmr_ingest_stock.samsung_metadata import attach,parse
    class HTTP:
        calls=0
        def get(self,url):self.calls+=1;raise ValueError('Missing notice')
    http=HTTP();d={'codename':'SM-A716U1','upstream_id':'sm-a716u1'};rows=[{'version':'A716U1UEU9GWF7','region':'XAA'}]
    assert attach(d,rows,http)==rows;attach(d,rows,http);assert http.calls==1
    page='<div>Build Number : A716U1UEU9GWF7</div><div>Android version : T(Android 13)</div><div>Release Date : 2023-07-18</div><div>Security patch level : 2023-06-01</div>'
    assert parse(page,'SM-A716U1','XAA','source')['A716U1UEU9GWF7']['android']=='13'


def test_provider_quota_stops_followup_and_settlement_requests(monkeypatch):
    from fmr_ingest_stock.d1_budget import BudgetDB,D1BudgetReached
    from fmr_core.d1_client import D1Client
    calls=[]
    def reject(*args,**kwargs):
        calls.append(1);raise RuntimeError("Your account has exceeded D1's free tier daily row write limit")
    monkeypatch.setattr(D1Client,'_request_cf_with_retry',reject)
    db=BudgetDB(local_sqlite_path=':memory:')
    try:
        db.metering=True
        for _ in range(2):
            with pytest.raises(D1BudgetReached):db._request_cf_with_retry({'sql':'SELECT 1'},True)
        db.settle();assert len(calls)==1
    finally:db.close()


def test_missing_official_notice_does_not_downgrade_verified_fields(env):
    from fmr_ingest_stock.metadata_publish import publish
    db,_=env;device,release=source();token=publish_release(db,device,release)['public_token']
    # The same logic applies to source outages after a verified Samsung update.
    original=json.loads(db.query('SELECT metadata_json FROM firmware_metadata')[0]['metadata_json'])
    original.update(android_version='13',published_date='2023-07-18',security_patch='2023-06-01')
    original['sources']['official_update']='https://doc.samsungmobile.com/source'
    db.execute('UPDATE firmware_metadata SET metadata_json=?,content_hash=?',(json.dumps(original),'old'))
    publish(db,token,device,{**release,'date':'2020-01-01','android_version':'Android 11'})
    saved=json.loads(db.query('SELECT metadata_json FROM firmware_metadata')[0]['metadata_json'])
    assert saved['android_version']=='13' and saved['published_date']=='2023-07-18' and saved['security_patch']=='2023-06-01'


@pytest.mark.parametrize('vendor',['xiaomi','google','samsung','oneplus','oppo','realme','vivo','asus','sony','motorola','nothing'])
def test_shared_metadata_contract_covers_every_active_vendor(vendor):
    device={'vendor':vendor,'upstream_id':'sample','codename':'sample'}
    release={'version':'BUILD.1','region':'Global','android_version':'Android 14','os_raw':'ColorOS 16','filename':'firmware.zip','date':'2026-01-01'}
    before=deepcopy(release);m=normalize(device,release)
    assert m['schema']==REVISION and m['android_version']=='14' and m['os_name']=='ColorOS' and m['os_version']=='16'
    assert m['original_filename']=='firmware.zip' and release==before


def test_google_real_caption_carrier_first_and_legacy_identity_are_safe(env):
    from fmr_ingest_stock.release_metadata import google_identity
    db,_=env
    device={'vendor':'google','upstream_id':'marlin','codename':'marlin','name':'Google Pixel XL'}
    release={'version':'7.1.0 (Verizon, NDE63X, Nov 2016)','region':'Global','kind':'factory','download_url':'https://dl.google.com/dl/android/aosp/marlin-nde63x-factory-8b520419.zip','android_version':'7.1.0'}
    assert google_identity(device,release)==('NDE63X','Verizon',['Verizon'])
    m=normalize(device,release)
    assert m['display_version']=='NDE63X' and m['region']=='Verizon'
    result=publish_release(db,device,release)
    row=db.query('SELECT * FROM stock_firmwares')[0]
    assert row['version_str']=='NDE63X' and row['region_code']=='Verizon'
    db.execute("UPDATE stock_firmwares SET version_str='Verizon',region_code='NDE63X'")
    before=db.query('SELECT * FROM stock_firmwares')[0]
    repeated=publish_release(db,device,release)
    assert repeated['public_token']==result['public_token']
    assert db.query('SELECT * FROM stock_firmwares')==[before]
    assert not repeated['metadata_changed']
    ota={**release,'download_url':'https://dl.google.com/dl/android/aosp/marlin-ota-qp1a.191005.007.a3-12345678.zip','version':'10.0.0 (QP1A.191005.007.A3, Oct 2019, Verizon)'}
    assert google_identity(device,ota)==('QP1A.191005.007.A3','Verizon',['Verizon'])


def test_nothing_os_numeric_source_is_not_android_and_can_repair_old_value(env):
    from fmr_ingest_stock.release_metadata import system_fields
    from fmr_ingest_stock.metadata_publish import publish
    db,_=env;device,release=source();device['vendor']='nothing'
    release.update(version='Spacewar-V3.2-260618-1045',android_version='3.2',os_raw='3.2',os_name='Nothing OS',os_version='3.2')
    fields=system_fields('3.2','nothing')
    assert fields['android_version']=='unknown' and fields['os_name']=='Nothing OS'
    assert system_fields('Android 15','nothing')['android_version']=='15'
    assert normalize(device,release)['android_version'] is None
    # Use a known published token, then simulate the legacy OS-as-Android value.
    d,r=source();token=publish_release(db,d,r)['public_token']
    db.execute("DELETE FROM firmware_metadata")
    db.execute("UPDATE stock_firmwares SET android_version='3.2'")
    assert publish(db,token,device,release)
    assert db.query('SELECT android_version FROM stock_firmwares')[0]['android_version']=='unknown'
    assert not publish(db,token,device,release)
    db.execute("UPDATE stock_firmwares SET android_version='15'")
    release['date']='2025-02-02';publish(db,token,device,release)
    assert db.query('SELECT android_version FROM stock_firmwares')[0]['android_version']=='15'


def test_realme_ui_and_sony_source_projection():
    from fmr_ingest_stock.release_metadata import system_fields
    d={'vendor':'realme','codename':'sample','upstream_id':'sample'}
    m=normalize(d,{'version':'build','android_version':'realme UI 16'})
    assert m['os_name']=='realme UI' and m['os_version']=='16' and m['android_version'] is None
    assert system_fields('realme UI 16','realme')['os_name']=='realme UI'
    d['vendor']='sony'
    m=normalize(d,{'version':'32.1.C.0.401-R19C','bundle':{'service':'https://app.swup.update.sony.net/service?password=private','cdf':'1295-0427','cust':'KDDI JP'}})
    assert m['sources']['catalogue']=='https://app.swup.update.sony.net/service'


def test_samsung_repeated_ap_dates_do_not_discard_other_official_builds():
    from fmr_ingest_stock.samsung_metadata import parse
    def block(ap,android,date,patch):
        return f'<div>Build Number : {ap}</div><div>Android version : T(Android {android})</div><div>Release Date : {date}</div><div>Security patch level : {patch}</div>'
    page=block('A716U1UEU9GWF7','13','2023-07-18','2023-06-01')+block('A716U1UEU6DVA1','11','2022-04-18','2022-01-01')+block('A716U1UEU6DVA1','11','2022-03-04','2022-01-01')
    result=parse(page,'SM-A716U1','XAA','source')
    assert result['A716U1UEU9GWF7']['android']=='13'
    assert result['A716U1UEU6DVA1']['android']=='11'
    assert result['A716U1UEU6DVA1']['date'] is None
    assert result['A716U1UEU6DVA1']['patch']=='2022-01-01'


def test_future_normalization_revision_does_not_reuse_retired_receipt(env,monkeypatch):
    from fmr_ingest_stock import release_metadata
    from fmr_ingest_stock.catalogue_store import digest
    db,_=env
    device,release=source()
    store=Store(db,'oneplus')
    key=store.key('release','revision-upgrade')
    store.put(key,{'device':device,'release':release,'status':'resolved'})
    ledger=publisher.open_ledger(':memory:')
    try:
        ledger.execute(publisher.ACK_SQL,('oneplus',key,digest([device,release,3])))
        assert list(publisher.candidates(db,ledger,'oneplus'))==[]
        monkeypatch.setattr(release_metadata,'NORMALIZATION_REVISION',4)
        pending=list(publisher.candidates(db,ledger,'oneplus'))
        assert len(pending)==1 and pending[0][1]==key
        assert pending[0][3]==digest([device,release,4])
        assert store.get(key)['release']==release
    finally:ledger.close()
