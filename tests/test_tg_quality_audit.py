import hashlib
import json
from pathlib import Path
import re
import pytest
from fmr_tg_scanner.quality_audit import audit,publish
from fmr_tg_scanner.search_policy import analyse
from fmr_tg_scanner.search_store import SearchStore
from fmr_tg_scanner.trial import atomic_json
from fmr_core.d1_client import D1Client
from fmr_core.crypto import generate_keypair


def make_message(i,text):
    return {'text':text,'channel_username':'testupdates','msg_id':i,'tg_post_url':f'https://t.me/testupdates/{i}',
            'published_timestamp':1793610000,'links':[],'link_items':[]}


def prepare(root,monkeypatch):
    from fmr_tg_scanner import quality_audit
    monkeypatch.setattr(quality_audit,'read_catalogue',lambda:({'alioth':['POCO F3']},[]))
    (root/'search-v3/logs').mkdir(parents=True)
    store=SearchStore(root/'search-v3/search.sqlite')
    entries=[]
    for i,extra in [(1,'By @alice_dev'),(2,'By @alice_dev\nPublic Releases Channel: @EliteDevelopment'),(3,'By @alice_dev')]:
        msg=make_message(i,'#STABLE #ROM #ALIOTH\nLineageOS v22.1 Unofficial | Android 15\n'+extra)
        result,reason=analyse(msg,'alioth',['POCO F3'],{'alioth':['POCO F3']})
        store.save(result['package'],result['evidence'],result['classification'],reason,True)
        if i!=3:entries.append({'event':'resource_decision','package':result['package'],'observed_text':msg['text']})
    (root/'search-v3/logs/record.jsonl').write_text('\n'.join(json.dumps(v) for v in entries)+'\n')
    store.close()
    return audit(root)


def test_full_audit_checks_every_row_and_quarantines_missing_evidence(tmp_path,monkeypatch):
    out=prepare(tmp_path/'state',monkeypatch)
    report=json.loads((out/'report.json').read_text())
    assert report['all_records_checked']==3 and report['qualified']==1
    assert report['rejected_or_incomplete']==2
    assert report['reason_counts']['source_text_not_recoverable']==1
    assert report['reason_counts']['excluded_elite_source']==1
    item=json.loads((out/'qualified.json').read_text())['products'][0]
    assert item['package']['taxonomy']['release_stage']=='stable'
    assert item['package']['build_version_tag']=='v22.1'
    store=SearchStore(tmp_path/'state/search-v3/search.sqlite')
    assert len(store.query('SELECT * FROM tg_search_records WHERE accepted=1'))==1
    store.close()


def test_publication_populates_existing_table_and_enables_device_idempotently(tmp_path,monkeypatch):
    out=prepare(tmp_path/'state',monkeypatch)
    db=D1Client(local_sqlite_path=str(tmp_path/'production.sqlite'))
    db.execute_script((Path(__file__).parents[1]/'schema.sql').read_text())
    db.execute("INSERT INTO brands(id,name,bl_unlock_status) VALUES ('xiaomi','Xiaomi','easy')")
    db.execute("INSERT INTO devices(codename,brand_id,marketing_name) VALUES ('alioth','xiaomi','POCO F3')")
    from fmr_core.schema_migrations import ensure_schema
    ensure_schema(db,allow_migration=True)
    private,_=generate_keypair()
    first=publish(out,db,private)
    assert first['published_and_verified']==1 and first['D1_rows_upserted']==1
    row=db.query('SELECT * FROM custom_resources')[0]
    assert re.fullmatch(r'cr_[A-Za-z0-9_-]{10,13}',row['public_token'])
    assert row['zone_type']=='free_rom' and row['target_post_url']=='https://t.me/testupdates/1'
    assert json.loads(row['metadata_json'])['taxonomy']['maintenance']['branch_id']
    assert db.query("SELECT phase2_roms_enabled FROM devices WHERE codename='alioth'")[0]['phase2_roms_enabled']==1
    db.execute('UPDATE custom_resources SET site_click_count=12')
    repeated=publish(out,db,private)
    assert repeated['published_and_verified']==1 and repeated['D1_rows_upserted']==0
    assert db.query('SELECT COUNT(*) n,MAX(site_click_count) clicks FROM custom_resources')[0]=={'n':1,'clicks':12}
    legacy='cr_'+hashlib.sha256(('alioth|https://t.me/testupdates/1').encode()).hexdigest()[:24]
    db.execute('UPDATE custom_resources SET public_token=?',(legacy,))
    assert publish(out,db,private)['published_and_verified']==1
    assert re.fullmatch(r'cr_[A-Za-z0-9_-]{10,13}',db.query('SELECT public_token FROM custom_resources')[0]['public_token'])
    db.close()


def test_channel_guesses_are_telegram_usernames_and_old_invalid_tasks_retire(tmp_path):
    from fmr_tg_scanner.search_discovery import name_candidates
    from fmr_tg_scanner.search_http import metadata_url
    from fmr_tg_scanner.frontier_repair import repair
    for code,names in [('asus-zenfone-8',['ASUS Zenfone 8','ZS590KS']),('google-panther',['Google Pixel 7','panther']),('asus-rog-phone-8-rog-be4a17f2b8',['ASUS ROG Phone 8'])]:
        values=name_candidates(code,names)
        assert values
        for value in values:assert metadata_url('https://t.me/s/'+value)
    db=SearchStore(tmp_path/'queue.sqlite')
    db.enqueue('admit','asus-zenfone-8',{'channel':'asus-zenfone-8'},3)
    db.enqueue('admit','alioth',{'channel':'aliothupdates'},3)
    assert repair(db)==1 and repair(db)==0
    assert db.due(['asus-zenfone-8']) is None
    assert db.due(['alioth']) is not None
    db.close()


def test_existing_site_pages_are_checked_against_the_full_publication_cohort(tmp_path,monkeypatch):
    import httpx
    from fmr_tg_scanner.quality_audit import resource_token,verify_site
    out=prepare(tmp_path/'state',monkeypatch)
    products=json.loads((out/'qualified.json').read_text())['products']
    token=resource_token(products[0]['package'])
    atomic_json(out/'publication_result.json',{'enabled_devices':['alioth']})
    def response(request):
        body=f'<a href="/verify/{token}">Visit release</a>' if '/devices/' in request.url.path else 'Verify release'
        return httpx.Response(200,text=body)
    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        assert verify_site(out,client=client)['verified_resources']==1
    with httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(200,text='Missing records'))) as client:
        with pytest.raises(RuntimeError,match='token set mismatch'):verify_site(out,client=client,sleeper=lambda _:None)


def test_tg_partial_commit_retry_preserves_old_rows_and_never_scans_unrelated_resources(tmp_path,monkeypatch):
    from copy import deepcopy
    out=prepare(tmp_path/'state',monkeypatch)
    template=json.loads((out/'qualified.json').read_text())['products'][0]
    products=[]
    for i in range(1,26):
        row=deepcopy(template);row['package']['msg_id']=100+i
        row['package']['tg_post_url']='https://t.me/testupdates/'+str(100+i)
        products.append(row)
    atomic_json(out/'qualified.json',{'products':products})
    db=D1Client(local_sqlite_path=str(tmp_path/'production.sqlite'))
    db.execute_script((Path(__file__).parents[1]/'schema.sql').read_text())
    from fmr_core.schema_migrations import ensure_schema
    ensure_schema(db,allow_migration=True,through=6)
    db.execute("INSERT INTO brands(id,name,bl_unlock_status) VALUES ('xiaomi','Xiaomi','easy')")
    db.execute("INSERT INTO devices(codename,brand_id,marketing_name) VALUES ('alioth','xiaomi','POCO F3')")
    db.execute("INSERT INTO custom_resources(public_token,device_codename,zone_type,category_tag,project_name,target_post_url,ed25519_signature,metadata_json) VALUES ('cr_unrelated01','alioth','free_rom','ROM','Unrelated','https://t.me/other/1','signature','{}')")
    key,_=generate_keypair();native=db.execute_batch;calls=0
    def interrupt(statements):
        nonlocal calls
        calls+=1
        if calls==2:raise RuntimeError('simulated network loss after first committed chunk')
        return native(statements)
    db.execute_batch=interrupt
    try:
        with pytest.raises(RuntimeError,match='network loss'):publish(out,db,key)
        assert not (out/'publication_result.json').exists()
        assert db.query('SELECT COUNT(*) n FROM custom_resources')[0]['n']==11
        assert db.query("SELECT 1 FROM custom_resources WHERE public_token='cr_unrelated01'")
        db.execute_batch=native;trace=[];db._get_sqlite_conn().set_trace_callback(trace.append)
        result=publish(out,db,key)
        assert result['published_and_verified']==25 and result['D1_rows_upserted']==15
        assert db.query('SELECT COUNT(*) n FROM custom_resources')[0]['n']==26
        assert not any(sql.strip().upper()=='SELECT * FROM CUSTOM_RESOURCES' for sql in trace)
        assert publish(out,db,key)['D1_rows_upserted']==0
    finally:db.close()


def test_website_verifier_follows_project_pages_and_both_resource_tabs(tmp_path,monkeypatch):
    import httpx
    from fmr_tg_scanner.quality_audit import verify_site
    out=prepare(tmp_path/'state',monkeypatch)
    a,b,c='cr_aaaaaaaaaa','cr_bbbbbbbbbb','cr_cccccccccc'
    atomic_json(out/'publication_result.json',{'enabled_devices':['alioth'],'resources_by_device':{'alioth':{'roms':[a,b],'recoveries':[c]}}})
    seen=[]
    def response(request):
        seen.append(str(request.url))
        if '/verify/' in request.url.path:return httpx.Response(200,text='Verify')
        token=c if request.url.params.get('tab')=='recoveries' else b if request.url.params.get('rpage')=='2' else a
        more='More projects →' if token==a else ''
        return httpx.Response(200,text=f'<a href="/verify/{token}">release</a><a href="/verify/cr_unrelated01">other</a>'+more)
    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        result=verify_site(out,client=client)
    assert result['verified_resources']==3 and result['verified_devices']==1
    assert any('rpage=2' in url for url in seen) and any('tab=recoveries' in url for url in seen)
