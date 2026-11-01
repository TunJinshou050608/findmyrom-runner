import json
import os
from pathlib import Path
import subprocess
import sys
import time

import httpx
import pytest
from fmr_core.d1_client import D1Client
from fmr_core.crypto import verify_signature,build_custom_resource_payload,generate_keypair
from fmr_tg_scanner import scan_channel,parse_tg_channel_page,extract_package_by_rules,export_to_custom_resources
from fmr_tg_scanner.crawler import PageFetcher,ScanHTTPError
from fmr_tg_scanner.html_parser import ChannelUnavailableError
from fmr_tg_scanner.cli import export_jsonl

ROOT=Path(__file__).resolve().parents[1]
LATEST=(ROOT/'tests/fixtures/tg01_page_latest.html').read_text()
OLD=(ROOT/'tests/fixtures/tg01_page_before_103.html').read_text()
EMPTY='<section class="tgme_widget_history"></section>'

@pytest.fixture
def db(tmp_path,monkeypatch):
    monkeypatch.setenv('FMR_TEST_NO_SLEEP','1')
    client=D1Client(local_sqlite_path=str(tmp_path/'tg.sqlite'))
    yield client
    client.close()


def pages(url):
    return OLD if '?before=103' in url else EMPTY if '?before=100' in url else LATEST


def test_deep_backfill_pagination_climbs_all_floors_to_bottom(db):
    requests=[]
    def fetch(url):requests.append(url);return pages(url)
    result=scan_channel(db,'@MarbleUpdates','marble',page_fetcher=fetch,enable_llm=False)
    assert requests==['https://t.me/s/marbleupdates','https://t.me/s/marbleupdates?before=103','https://t.me/s/marbleupdates?before=100']
    assert result['min_scanned_msg_id']==100 and result['max_scanned_msg_id']==105 and result['history_exhausted']
    assert result['packages_saved']==5 and result['skipped_noise_count']==1
    requests.clear()
    result=scan_channel(db,'marbleupdates','marble',page_fetcher=fetch,enable_llm=False)
    assert len(requests)==1 and result['messages_inspected']==0 and result['packages_saved']==0


def test_author_direct_vs_native_forward_vs_repost_attribution(db):
    scan_channel(db,'marbleupdates','marble',max_pages=1,page_fetcher=pages)
    rows=db.query('SELECT * FROM tg_discovered_packages ORDER BY msg_id DESC')
    assert [r['origin_type'] for r in rows]==['author_direct','native_forward','repost_aggregator']
    assert rows[0]['maintainer_handle']=='@ralf_dev' and rows[0]['tg_post_url']=='https://t.me/marbleupdates/105'
    assert rows[1]['original_post_url']=='https://t.me/orangefoxupdates/88' and rows[1]['is_forwarded']==1
    assert rows[1]['resource_kind']=='recovery' and rows[1]['maintainer_handle']=='@darthjabba9'
    assert rows[2]['original_post_url']=='https://t.me/crdroidmarble/42' and rows[2]['maintainer_handle']=='@gwolf2u'


def test_direct_tg_document_attachment_package_detection():
    msg=parse_tg_channel_page(OLD,'marbleupdates')['messages'][0]
    resource=extract_package_by_rules(msg)
    assert resource['resource_kind']=='boot_image' and resource['has_tg_attachment']==1
    assert json.loads(resource['download_links_json'])==['https://t.me/marbleupdates/102']
    assert resource['file_attachment_name']=='boot_magisk_v28.img.gz'


def test_rule_engine_handles_standard_posts_with_zero_llm_calls(db):
    html='<section class="tgme_widget_history">'+''.join(f'<div class="tgme_widget_message" data-post="TestChannel/{i}"><div class="tgme_widget_message_text">Evolution X v9.5 #marble Android 15 By @ralf_dev https://sourceforge.net/a.zip</div></div>' for i in range(1,6))+'</section>'
    def forbidden(_):raise AssertionError('Standard releases must not call AI')
    result=scan_channel(db,'testchannel',page_fetcher=lambda _:html,llm_callable=forbidden)
    assert result['packages_saved']==5 and result['llm_calls_used']==0


def test_llm_fallback_triggered_only_on_low_confidence_and_respects_budget(db):
    calls=[]
    def llm(text):calls.append(text);return {'device_codename':'marble','project_name':'Mystery ROM'}
    result=scan_channel(db,'marbleupdates',page_fetcher=pages,llm_callable=llm)
    assert result['llm_calls_used']==1 and len(calls)==1
    assert db.query('SELECT extraction_method FROM tg_discovered_packages WHERE msg_id=101')[0]['extraction_method']=='rule_plus_llm'
    low='<section class="tgme_widget_history">'+''.join(f'<div class="tgme_widget_message" data-post="BudgetChannel/{i}"><div class="tgme_widget_message_text">New flashable zip build {i} here https://mega.nz/file/{i}</div></div>' for i in range(1,6))+'</section>'
    result=scan_channel(db,'budgetchannel','marble',page_fetcher=lambda _:low,llm_callable=llm,max_llm_calls=2)
    assert result['llm_calls_used']==2
    # Same content reposted at a new floor reuses the prior parsed metadata.
    duplicate=OLD.replace('MarbleUpdates/101','DuplicateChan/1').replace('MarbleUpdates/102','InvalidChannel/2').replace('MarbleUpdates/100','InvalidChannel/3')
    result=scan_channel(db,'duplicatechan',page_fetcher=lambda _:duplicate,llm_callable=llm)
    assert result['llm_calls_used']==0


def test_llm_failure_gracefully_degrades_to_rule_only_without_losing_cursor(db):
    def failure(_):raise httpx.ReadTimeout('synthetic failure')
    result=scan_channel(db,'marbleupdates','marble',page_fetcher=pages,llm_callable=failure)
    assert result['min_scanned_msg_id']==100 and result['packages_saved']==5
    assert db.query('SELECT extraction_method FROM tg_discovered_packages WHERE msg_id=101')[0]['extraction_method']=='rule_only'


def test_export_jsonl_and_bridge_sync_to_sp07_custom_resources(db,tmp_path,monkeypatch):
    scan_channel(db,'marbleupdates','marble',page_fetcher=pages,enable_llm=False)
    file=tmp_path/'out.jsonl';assert export_jsonl(db,'marbleupdates',file)
    before=file.stat().st_mtime_ns;assert not export_jsonl(db,'marbleupdates',file);assert before==file.stat().st_mtime_ns
    assert len(file.read_text().splitlines())==5
    db.execute_script((ROOT/'schema.sql').read_text())
    db.execute("INSERT INTO brands(id,name,bl_unlock_status) VALUES ('xiaomi','Xiaomi','easy')")
    db.execute("INSERT INTO devices(codename,brand_id,marketing_name) VALUES ('marble','xiaomi','POCO F5')")
    private,public=generate_keypair();monkeypatch.setenv('SIGN_PRIVATE_KEY_HEX',private)
    assert export_to_custom_resources(db)['inserted']==5
    assert export_to_custom_resources(db)['unchanged']==5
    for row in db.query('SELECT * FROM custom_resources'):
        assert verify_signature(build_custom_resource_payload(row['public_token'],row['zone_type'],row['target_post_url']),row['ed25519_signature'],public)
    row=db.query("SELECT target_post_url FROM custom_resources WHERE project_name='OrangeFox'")[0]
    assert row['target_post_url']=='https://t.me/orangefoxupdates/88'


def simple_page(channel,ids):
    return '<section class="tgme_widget_history">'+''.join(f'<div class="tgme_widget_message" data-post="{channel}/{i}"><div class="tgme_widget_message_text">Evolution X v9.5 #marble Android 15 By @ralf_dev https://sourceforge.net/{i}.zip</div></div>' for i in ids)+'</section>'


def test_incremental_gap_survives_budget_and_restart_without_skipping(db):
    scan_channel(db,'gapchannel','marble',max_pages=1,page_fetcher=lambda _:simple_page('gapchannel',[10,9]))
    requested=[]
    def fetch(url):
        requested.append(url)
        if '?before=29' in url:return simple_page('gapchannel',[28,27])
        if '?before=27' in url:return simple_page('gapchannel',[26,10])
        return simple_page('gapchannel',[30,29])
    scan_channel(db,'gapchannel','marble',max_pages=1,direction='incremental',page_fetcher=fetch)
    scan_channel(db,'gapchannel','marble',max_pages=1,direction='incremental',page_fetcher=fetch)
    assert requested[-1].endswith('?before=29')
    scan_channel(db,'gapchannel','marble',max_pages=1,direction='incremental',page_fetcher=fetch)
    assert requested[-1].endswith('?before=27')
    row=db.query('SELECT * FROM tg_scan_cursors')[0]
    assert row['max_scanned_msg_id']==30 and row['min_scanned_msg_id']==9
    assert {r['msg_id'] for r in db.query('SELECT msg_id FROM tg_discovered_packages')}=={9,10,26,27,28,29,30}


def test_failed_page_retains_previous_commit_and_private_channel_never_marks_done(db):
    def fetch(url):
        if '?' in url:raise ScanHTTPError('synthetic failure')
        return LATEST
    with pytest.raises(ScanHTTPError):scan_channel(db,'marbleupdates','marble',page_fetcher=fetch)
    assert db.query('SELECT min_scanned_msg_id FROM tg_scan_cursors')[0]['min_scanned_msg_id']==103
    before=db.query('SELECT * FROM tg_scan_cursors')
    with pytest.raises(ChannelUnavailableError):scan_channel(db,'marbleupdates','marble',direction='backfill',page_fetcher=lambda _:'<html>Private channel</html>')
    assert before==db.query('SELECT * FROM tg_scan_cursors')


def test_urls_no_intranet_and_algorithm_20_floors_under_100ms(db):
    html=simple_page('speedchannel',range(1,21))
    started=time.perf_counter();result=scan_channel(db,'speedchannel','marble',page_fetcher=lambda _:html,enable_llm=False)
    assert time.perf_counter()-started<=.1
    assert result['packages_saved']==20
    msg=parse_tg_channel_page(LATEST,'marbleupdates')['messages'][0]
    msg.update(text='Android ROM for #marble http://192.168.1.1/rom.zip',links=[])
    assert extract_package_by_rules(msg) is None
    assert parse_tg_channel_page('<section class="tgme_widget_history"><div class="tgme_widget_message" data-post="Elsewhere/5"></div></section>','marbleupdates')['messages']==[]


def test_http_retries_bounded_and_no_download_urls_are_requested(monkeypatch):
    monkeypatch.setenv('FMR_TEST_NO_SLEEP','1');urls=[]
    def handler(req):urls.append(str(req.url));return httpx.Response(429,headers={'Retry-After':'0'})
    fetch=PageFetcher();fetch.client.close();fetch.client=httpx.Client(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ScanHTTPError):fetch('https://t.me/s/marbleupdates')
        assert len(urls)==3 and set(urls)=={'https://t.me/s/marbleupdates'}
    finally:fetch.close()


def test_private_checkpoint_uploads_before_deletion_and_unchanged_is_zero_upload(tmp_path):
    from fmr_tg_scanner.cloud_job import PrivateCheckpoint
    import hashlib
    file=tmp_path/'state.sqlite';file.write_bytes(b'synthetic state')
    digest=hashlib.sha256(file.read_bytes()).hexdigest();calls=[]
    def handler(request):
        calls.append((request.method,str(request.url)))
        return httpx.Response(201 if request.method=='POST' else 204, json={'id':99} if request.method=='POST' else None)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        checkpoint=PrivateCheckpoint(client,'test/private-vault','marbleupdates')
        assert not checkpoint.save(file,digest,{'id':1},[])
        assert calls==[]
        assert checkpoint.save(file,None,{'id':1},[{'name':'tg01-marbleupdates.sqlite.'+'a'*64,'id':4}])
        assert [c[0] for c in calls]==['POST','DELETE']
        assert 'uploads.github.com' in calls[0][1]


def test_checkpoint_upload_failure_never_deletes_last_good_generation(tmp_path):
    from fmr_tg_scanner.cloud_job import PrivateCheckpoint
    file=tmp_path/'state.sqlite';file.write_bytes(b'synthetic');calls=[]
    def handler(request):calls.append(request.method);return httpx.Response(500)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            PrivateCheckpoint(client,'test/private-vault','marbleupdates').save(file,None,{'id':1},[{'name':'tg01-marbleupdates.sqlite.'+'a'*64,'id':4}])
    assert calls==['POST']


def test_offline_cli_and_limits(tmp_path):
    env=dict(os.environ,LOCAL_SQLITE_PATH=str(tmp_path/'cli.db'),FMR_TEST_NO_SLEEP='1')
    cmd=[sys.executable,'-m','fmr_tg_scanner.cli','--channel','@MarbleUpdates','--default-codename','marble','--no-llm','--offline-pages','tests/fixtures/tg01_page_latest.html,tests/fixtures/tg01_page_before_103.html','--export-jsonl',str(tmp_path/'out.jsonl')]
    result=subprocess.run(cmd,cwd=ROOT,env=env,text=True,capture_output=True)
    assert result.returncode==0,result.stderr
    summary=json.loads(result.stdout.splitlines()[-1]);assert summary['packages_saved']==5
    assert summary['max_scanned_msg_id']==105 and summary['min_scanned_msg_id']==100
    assert len((tmp_path/'out.jsonl').read_text().splitlines())==5
    result=subprocess.run(cmd+['--max-pages','5001'],cwd=ROOT,env=env,text=True,capture_output=True)
    assert result.returncode==2


def test_stalled_incremental_page_does_not_skip_catchup_gap(db):
    scan_channel(db,'gapchannel','marble',max_pages=1,page_fetcher=lambda _:simple_page('gapchannel',[10,9]))
    fetch=lambda _:simple_page('gapchannel',[30,29])
    scan_channel(db,'gapchannel','marble',max_pages=1,direction='incremental',page_fetcher=fetch)
    before=db.query('SELECT * FROM tg_scan_cursors')
    with pytest.raises(ScanHTTPError):scan_channel(db,'gapchannel','marble',max_pages=1,direction='incremental',page_fetcher=fetch)
    assert db.query('SELECT * FROM tg_scan_cursors')==before


def test_ai_attempt_intent_survives_crash_without_repeated_billing(db):
    class SimulatedKill(BaseException):pass
    calls=[]
    def llm(text):calls.append(text);raise SimulatedKill()
    with pytest.raises(SimulatedKill):scan_channel(db,'marbleupdates','marble',page_fetcher=pages,llm_callable=llm)
    row=db.query('SELECT * FROM tg_scan_cursors')[0]
    assert row['min_scanned_msg_id']==103
    result=scan_channel(db,'marbleupdates','marble',direction='backfill',page_fetcher=pages,llm_callable=llm)
    assert result['llm_calls_used']==0 and len(calls)==1
    assert db.query('SELECT COUNT(*) AS n FROM tg_discovered_packages')[0]['n']==5


def test_private_checkpoint_restore_is_verified_and_reuses_existing_cursor(db,tmp_path):
    from fmr_tg_scanner.cloud_job import PrivateCheckpoint
    from fmr_tg_scanner.storage import SQLiteDB
    import hashlib
    scan_channel(db,'marbleupdates','marble',max_pages=1,page_fetcher=pages)
    raw=Path(db.local_path).read_bytes();digest=hashlib.sha256(raw).hexdigest()
    def handler(request):
        url=str(request.url)
        if '/releases/tags/' in url:return httpx.Response(200,json={'id':1})
        if '/releases/1/assets?' in url:return httpx.Response(200,json=[{'id':9,'name':'tg01-marbleupdates.sqlite.'+digest}])
        if '/releases/assets/9' in url:return httpx.Response(200,content=raw)
        raise AssertionError(url)
    restored=tmp_path/'restored.sqlite'
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        got,info,assets=PrivateCheckpoint(client,'owner/private-vault','marbleupdates').restore(restored)
        assert got==digest
        copied=SQLiteDB(restored)
        try:
            urls=[]
            def fetch(url):urls.append(url);return pages(url)
            result=scan_channel(copied,'marbleupdates','marble',direction='backfill',page_fetcher=fetch,enable_llm=False)
            assert urls[0].endswith('?before=103') and result['packages_saved']==2
        finally:copied.close()


def test_independent_storage_rolls_back_failed_page(tmp_path):
    from fmr_tg_scanner.storage import SQLiteDB
    db=SQLiteDB(tmp_path/'independent.sqlite')
    try:
        db.execute('CREATE TABLE tx(id INTEGER PRIMARY KEY)')
        with pytest.raises(Exception):db.execute_batch([('INSERT INTO tx VALUES (?)',(1,)),('INSERT INTO missing VALUES (?)',(2,))])
        assert db.query('SELECT * FROM tx')==[]
        result=scan_channel(db,'marbleupdates','marble',page_fetcher=pages,enable_llm=False)
        assert result['packages_saved']==5
    finally:db.close()


def test_link_only_catalogue_records_package_and_sender_without_inventing_id():
    html=LATEST.replace('<span class="tgme_widget_message_author">@ralf_dev</span>','<span class="tgme_widget_message_author"><a href="https://t.me/actual_poster">Actual poster</a></span>')
    msg=parse_tg_channel_page(html,'marbleupdates')['messages'][0]
    record=extract_package_by_rules(msg)
    assert record['package_name']=='marble.zip'
    assert record['tg_post_url']=='https://t.me/marbleupdates/105'
    assert record['maintainer_handle']=='@ralf_dev'
    assert record['sender_username']=='@actual_poster' and record['sender_id'] is None
    msg['sender_id']=12345678
    assert extract_package_by_rules(msg)['sender_id']==12345678
    msg['sender_id']='@ralf_dev'
    assert extract_package_by_rules(msg)['sender_id'] is None
    attachment=extract_package_by_rules(parse_tg_channel_page(OLD,'marbleupdates')['messages'][0])
    assert attachment['package_name']=='boot_magisk_v28.img.gz'


def test_page_fetcher_cannot_download_firmware_or_tg_attachment():
    fetch=PageFetcher();requests=[]
    fetch.client.close()
    fetch.client=httpx.Client(transport=httpx.MockTransport(lambda request:requests.append(str(request.url))))
    try:
        for url in ['https://sourceforge.net/projects/rom/files/rom.zip','https://t.me/marbleupdates/102','https://t.me/s/marbleupdates?before=5&download=rom.zip','https://t.me.evil.example/s/marbleupdates']:
            with pytest.raises(ValueError):fetch(url)
        assert requests==[]
    finally:fetch.close()


def test_metadata_columns_migrate_once_without_rescanning_history(db):
    from fmr_tg_scanner.schema import initialize
    scan_channel(db,'marbleupdates','marble',max_pages=1,page_fetcher=pages,enable_llm=False)
    snapshot=db.query('SELECT * FROM tg_scan_cursors')
    for column in ['package_name','sender_id','sender_username']:db.execute('ALTER TABLE tg_discovered_packages DROP COLUMN '+column)
    initialize(db)
    assert db.query('SELECT * FROM tg_scan_cursors')==snapshot
    assert all(row['package_name'] for row in db.query('SELECT * FROM tg_discovered_packages'))
    before=db._get_sqlite_conn().total_changes
    initialize(db)
    assert before==db._get_sqlite_conn().total_changes


def test_live_dom_container_and_annibale_target_do_not_turn_project_tags_into_devices(db):
    html='<section class="tgme_channel_history js-message_history"><div class="tgme_widget_message" data-post="MixUpdates/10"><div class="tgme_widget_message_text">New custom ROM Project Infinity X for POCO F8 PRO [ANNIBALE] #ProjectInfinityX #SMGTeams #annibale By @dev_name https://sourceforge.net/new.zip</div></div><div class="tgme_widget_message" data-post="MixUpdates/9"><div class="tgme_widget_message_text">crDroid v12 for #whyred Android 16 https://sourceforge.net/whyred.zip</div></div></section>'
    events=[]
    result=scan_channel(db,'mixupdates',max_pages=1,page_fetcher=lambda _:html,target_codenames={'annibale'},event_sink=events.append,enable_llm=False)
    assert result['packages_saved']==1
    assert db.query('SELECT device_codename FROM tg_discovered_packages')==[{'device_codename':'annibale'}]
    assert any(e.get('reason')=='target_mismatch' for e in events)
    assert sum(e['event']=='package_committed' for e in events)==1


def test_trial_official_policy_keeps_unofficial_before_matching_official_word():
    from fmr_tg_scanner.trial import classify
    package={'project_name':'LineageOS','download_links_json':'["https://sourceforge.net/lineage.zip"]'}
    assert classify(package,'LineageOS 24 UNOFFICIAL build for annibale')=='unofficial'
    assert classify(package,'LineageOS 24 OFFICIAL build for annibale')=='mainstream_official'
    assert classify(package,'LineageOS 24 build for annibale')=='unknown'


def test_live_trial_saves_private_log_export_audit_and_resumes(tmp_path,monkeypatch):
    from fmr_tg_scanner.trial import run
    monkeypatch.setenv('GITHUB_RUN_ID','test-run');monkeypatch.setenv('FMR_TEST_NO_SLEEP','1')
    (tmp_path/'config.json').write_text(json.dumps({'target_codenames':['marble'],'pages_per_channel':3,'channels_per_batch':1,'discover_linked_channels':False}))
    report=run(tmp_path,fetcher=pages,discoverer=lambda device,cache,log:[{'channel':'marbleupdates'}])
    assert report['ok'] and report['packages']==4
    assert (tmp_path/'state.sqlite').exists() and (tmp_path/'packages/marbleupdates.jsonl').exists()
    log=(tmp_path/'logs/test-run-1.jsonl').read_text();assert 'page_fetch_start' in log and 'message_skipped' in log and 'package_committed' in log
    assert 'no_resource_evidence' in log
    monkeypatch.setenv('GITHUB_RUN_ID','second-run')
    second=run(tmp_path,fetcher=pages,discoverer=lambda device,cache,log:[{'channel':'marbleupdates'}])
    assert second['ok'] and second['packages']==4 and second['batch_results']==[]


def test_autonomous_discovery_only_needs_codename_and_caches_search(monkeypatch):
    from fmr_tg_scanner import discovery
    requests=[];events=[]
    monkeypatch.setattr(discovery,'SEARCH_PROVIDERS',[('search','https://search.example/search','q')])
    def handler(request):
        requests.append(str(request.url))
        if request.url.host=='search.example':
            assert 'annibale' in request.url.params['q']
            return httpx.Response(200,text='<a href="https://t.me/AutoFoundChannel/73">annibale ROM</a>')
        if request.url.host=='api.github.com':return httpx.Response(200,json={'items':[]})
        if request.url.host=='t.me':return httpx.Response(200,text='<html>Not a public preview</html>')
        raise AssertionError(str(request.url))
    cache={}
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result=discovery.discover_channels('annibale',cache,events.append,client=client,now=10)
        assert [r['channel'] for r in result]==['autofoundchannel']
        count=len(requests)
        again=discovery.discover_channels('annibale',cache,events.append,client=client,now=11)
        assert again==result and len(requests)==count
    assert any(e['event']=='discovery_cache_hit' for e in events)


def test_autonomous_trial_rejects_human_channel_seeds(tmp_path):
    from fmr_tg_scanner.trial import run
    (tmp_path/'config.json').write_text(json.dumps({'target_codenames':['annibale'],'seed_channels':['manualhelp']}))
    with pytest.raises(ValueError,match='Manual channel seeds'):run(tmp_path,fetcher=lambda _:EMPTY)


def test_no_sources_is_a_failed_trial_not_a_success(tmp_path,monkeypatch):
    from fmr_tg_scanner.trial import run
    monkeypatch.setenv('GITHUB_RUN_ID','no-sources')
    (tmp_path/'config.json').write_text(json.dumps({'target_codenames':['annibale']}))
    report=run(tmp_path,fetcher=lambda _:EMPTY,discoverer=lambda *_:[])
    assert not report['ok'] and report['source_count']==0
    assert {'reason':'autonomous_discovery_found_no_sources'} in report['errors']


def test_public_profile_redirect_reads_only_same_account_metadata(monkeypatch):
    monkeypatch.setenv('FMR_TEST_NO_SLEEP','1');urls=[]
    def handler(req):
        urls.append(str(req.url))
        if req.url.path.startswith('/s/'):
            return httpx.Response(302,headers={'Location':'https://t.me/example_profile'})
        return httpx.Response(200,text='<div>Channel: @auto_updates</div>')
    fetch=PageFetcher();fetch.client.close();fetch.client=httpx.Client(transport=httpx.MockTransport(handler))
    try:
        assert 'auto_updates' in fetch('https://t.me/s/example_profile')
        assert urls==['https://t.me/s/example_profile','https://t.me/example_profile']
    finally:fetch.close()


def test_public_profile_redirect_cannot_fetch_a_file_or_other_host(monkeypatch):
    monkeypatch.setenv('FMR_TEST_NO_SLEEP','1')
    for destination in ['http://127.0.0.1/file','https://t.me/example_profile/17','https://evil.example/rom.zip','https://t.me/']:
        urls=[]
        def handler(req):urls.append(str(req.url));return httpx.Response(302,headers={'Location':destination})
        fetch=PageFetcher();fetch.client.close();fetch.client=httpx.Client(transport=httpx.MockTransport(handler))
        try:
            with pytest.raises(ChannelUnavailableError):fetch('https://t.me/s/example_profile')
            assert len(urls)==1
        finally:fetch.close()


def test_search_discovery_rejects_unrelated_same_name_results():
    from fmr_tg_scanner.discovery import organic_candidates
    html='''<a class="result-link" href="https://t.me/music_only">Annibale concert</a><td class="result-snippet">Annibale music in Roma</td><a class="result-link" href="https://t.me/android_updates">annibale PixelOS ROM</a><td class="result-snippet">Android 17 unofficial builds for annibale</td>'''
    rows=organic_candidates(html,'annibale')
    assert [r['channel'] for r in rows]==['android_updates']
    assert rows[0]['quality_score']==200


def test_device_directory_links_do_not_count_as_real_packages():
    from fmr_tg_scanner.trial import qualification,classify
    row={'package_name':'Phone Name','file_attachment_name':None,'build_version_tag':None,'project_name':'HyperOS','rule_confidence':1.0,'download_links_json':'["https://mirror.example/phones/some-phone"]'}
    assert qualification(row)=='index_reference'
    assert classify(row)=='stock_official'
    row['download_links_json']='["https://mirror.example/ROM-v1.2.zip"]'
    assert qualification(row)=='package'


def test_telegram_android_footer_does_not_expand_unrelated_channels(tmp_path,monkeypatch):
    from fmr_tg_scanner.trial import run
    monkeypatch.setenv('GITHUB_RUN_ID','irrelevant-footer');monkeypatch.setenv('FMR_TEST_NO_SLEEP','1')
    (tmp_path/'config.json').write_text(json.dumps({'target_codenames':['annibale']}))
    html='<section class="tgme_channel_history"><div class="tgme_widget_message" data-post="MusicChannel/1"><div class="tgme_widget_message_text">Concert tonight https://t.me/unrelated_music</div></div></section><footer>Telegram for Android</footer>'
    report=run(tmp_path,fetcher=lambda _:html,discoverer=lambda *_:[{'channel':'musicchannel'}])
    registry=json.loads((tmp_path/'source_registry.json').read_text())
    assert 'unrelated_music' not in registry
    assert registry['musicchannel']['phone_context'] is False
    assert not report['ok']


def test_device_alias_lookup_derives_channels_without_human_model_or_channel_inputs(monkeypatch):
    from fmr_tg_scanner import discovery
    monkeypatch.setenv('FMR_TEST_NO_SLEEP','1');monkeypatch.setattr(discovery,'SEARCH_PROVIDERS',[])
    requested=[]
    def handler(req):
        requested.append(str(req.url))
        if req.url.host=='raw.githubusercontent.com':return httpx.Response(200,text='testdevice: ["Brand Q7 Pro / Brand Q7", "Global"]')
        if req.url.host=='t.me':
            channel=req.url.path.rsplit('/',1)[-1]
            if channel=='brandq7update':return httpx.Response(200,text=simple_page(channel,[2,1]))
            return httpx.Response(200,text='<html>No history</html>')
        if req.url.host=='api.github.com':return httpx.Response(200,json={'items':[]})
        return httpx.Response(503)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        found=discovery.discover_channels('testdevice',{},lambda *_:None,client=client)
    assert any(item['channel']=='brandq7update' for item in found)
    assert 'https://t.me/s/brandq7update' in requested


def test_sourceforge_download_page_preserves_actual_package_name():
    from fmr_tg_scanner.trial import qualification
    message=parse_tg_channel_page(LATEST,'marbleupdates')['messages'][0]
    message['links']=['https://sourceforge.net/projects/test/files/Android-ROM-v2.0.zip/download']
    row=extract_package_by_rules(message)
    assert row['package_name']=='Android-ROM-v2.0.zip'
    assert qualification(row)=='package'


def test_rom_heading_wins_over_companion_recovery_and_non_download_links():
    html='''<section class="tgme_widget_history"><div class="tgme_widget_message" data-post="TestChannel/5"><div class="tgme_widget_message_text">AxionOS v2.8 Unofficial ROM for #marble Android 16<br>By @dev_name<br><a href="https://github.com/AxionAOSP/axion_features">Features</a><a href="https://telegra.ph/guide">Installation</a><a href="https://sourceforge.net/projects/test/files/OrangeFox-R11.3.img/download">Recovery</a><a href="https://pixeldrain.com/u/romfile">ROM</a><a href="https://pixeldrain.com/u/bootfile">Boot images</a><a href="https://linktr.ee/author">Support</a></div></div></section>'''
    row=extract_package_by_rules(parse_tg_channel_page(html,'testchannel')['messages'][0])
    assert row['project_name']=='AxionOS' and row['resource_kind']=='rom'
    assert row['build_version_tag']=='v2.8'
    assert 'OrangeFox' not in row['package_name']
    assert json.loads(row['download_links_json'])==['https://pixeldrain.com/u/romfile']


def test_versioned_metadata_repair_does_not_reset_or_repeat_history(db):
    from fmr_tg_scanner.trial import repair_existing,RULE_REVISION
    scan_channel(db,'marbleupdates','marble',max_pages=1,page_fetcher=pages,enable_llm=False)
    before=db.query('SELECT * FROM tg_scan_cursors')
    registry={};calls=[]
    def fetch(url):calls.append(url);return LATEST
    repair_existing(db,registry,fetch,{'marble'},lambda *_:None)
    assert len(calls)==1
    assert db.query('SELECT * FROM tg_scan_cursors')==before
    assert registry['marbleupdates']['rule_revision']==RULE_REVISION
    repair_existing(db,registry,fetch,{'marble'},lambda *_:None)
    assert len(calls)==1


def test_metadata_repair_failure_is_not_marked_complete_and_has_retry_cooldown(db):
    from fmr_tg_scanner.trial import repair_existing
    scan_channel(db,'marbleupdates','marble',max_pages=1,page_fetcher=pages,enable_llm=False)
    registry={};calls=[]
    def failed(url):calls.append(url);raise ScanHTTPError('temporary metadata error')
    repair_existing(db,registry,failed,{'marble'},lambda *_:None)
    assert 'rule_revision' not in registry['marbleupdates']
    repair_existing(db,registry,failed,{'marble'},lambda *_:None)
    assert len(calls)==1
