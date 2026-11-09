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


def source():
    return {'vendor':'oneplus','upstream_id':'sample','codename':'sample','name':'Sample','page':'https://fwatlas.com/oneplus/sample'}, {'version':'CPH1234_15.0.1(EX01)','region':'EU','android_version':'OxygenOS 15','kind':'full_ota','date':'2025-01-01','size':'1 GB','download_url':'https://ota.oneplus.net/sample.zip','origin_kind':'oem','resolution':'resolved'}


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
