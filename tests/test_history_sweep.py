"""Generated metadata/bytes only. No live device records or firmware downloads."""
from pathlib import Path
import httpx
import pytest
from fmr_core.d1_client import D1Client
from fmr_ingest_stock.device_sync import ensure_base_brands_and_anchor
from fmr_ingest_stock.history import INDEX, discover_device, parse_detail
from fmr_extractor.history_sweep import ScopedDB, choose_batch, main, report, save


def detail(version='V1.0.0.0.TXXMIXM', code='sample', kind='Recovery', filename=None, suffix=''):
    filename = filename or ('miui_SAMPLE_'+version+'_aaaaaaaaaa_13.0.zip' if kind == 'Recovery' else 'sample_images_'+version+'_13.0_aaaaaaaaaa.tgz')
    return f'''<ul><li>Device: Sample Global</li><li>Codename: {code}</li><li>Version: {version}</li>
    <li>Branch: Stable</li><li>Type: {kind}</li><li>Size: 6.0 GB</li><li>Release Date: 2023-01-01</li>
    <li>Package Name: {filename}</li><li>MD5: {'a'*32}</li><li>Download:
    <button id="incremental_download" onclick="window.open('https://bigota.d.miui.com/evil/blockota.zip', '_blank');">Incremental</button>
    <button id="download" onclick="window.open('https://bigota.d.miui.com/{version}/{filename}{suffix}', '_blank');">Full</button>
    </li></ul>'''


def test_parser_distinguishes_full_roms_incrementals_and_mismatched_devices():
    rows, rejected = parse_detail(detail()+detail(code='unrelated'), 'sample')
    assert len(rows) == 1 and rejected == 1
    assert 'blockota' not in rows[0]['link']
    assert rows[0]['codename'] == 'sample_global' and rows[0]['android'] == '13.0'
    for filename in ['fw_sample_13.0.zip','blockota_sample_13.0.zip']:
        assert not parse_detail(detail(filename=filename),'sample')[0]
    assert not parse_detail(detail(suffix="/wrong"),'sample')[0]
    html = detail().replace('bigota.d.miui.com','attacker.example.com')
    assert not parse_detail(html,'sample')[0]


def test_discovers_miui_and_hyperos_history_pagination_and_old_versions():
    routes = {
        '/archive/miui/sample/': '<a href="/miui/sample/stable/V1.0.0.0.TXXMIXM/">old</a><a href="?page=2">next</a>',
        '/archive/miui/sample/?page=2': '<a href="/miui/sample/public%20beta/V2.0.0.0.TXXCNXM/">beta</a>',
        '/archive/hyperos/sample/': '<a href="/hyperos/sample/stable/OS3.0.0.0.WXXCNXM/">new</a>',
        '/miui/sample/stable/V1.0.0.0.TXXMIXM/': detail(),
        '/miui/sample/public%20beta/V2.0.0.0.TXXCNXM/': detail(version='V2.0.0.0.TXXCNXM'),
        '/hyperos/sample/stable/OS3.0.0.0.WXXCNXM/': detail(version='OS3.0.0.0.WXXCNXM',filename='sample-ota_full-OS3.0.0.0.WXXCNXM-user-16.0-aaaaaaaaaa.zip'),
    }
    def handler(request):
        path = request.url.raw_path.decode()
        return httpx.Response(200,text=routes[path]) if path in routes else httpx.Response(404)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        rows, manifest = discover_device('sample',client)
    assert len(rows) == 3 and manifest['discovered_pages'] == 3 and not manifest['errors']
    assert {r['android'] for r in rows} == {'13.0','16.0'}


def test_absent_history_and_detail_failures_are_not_silent_success():
    with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(404))) as client:
        rows, manifest = discover_device('sample',client)
    assert not rows and manifest['errors'][-1]['error'] == 'NoHistoryFound'
    def broken(request):
        if '/archive/miui/' in str(request.url):
            return httpx.Response(200,text='<a href="/miui/sample/stable/V1/">one</a>')
        return httpx.Response(404)
    with httpx.Client(transport=httpx.MockTransport(broken)) as client:
        rows, manifest = discover_device('sample',client)
    assert not rows and any(e['stage'] == 'detail' for e in manifest['errors'])


def test_conflicting_packages_are_retained_in_the_private_manifest():
    html = detail() + detail(kind='Fastboot') + detail(kind='Fastboot',filename='sample_images_V1.0.0.0.TXXMIXM.debug_13.0_aaaaaaaaaa.tgz')
    def handler(request):
        if '/archive/miui/' in str(request.url):
            return httpx.Response(200,text='<a href="/miui/sample/stable/V1/">one</a>')
        if '/archive/hyperos/' in str(request.url):
            return httpx.Response(404)
        return httpx.Response(200,text=html)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        rows, manifest = discover_device('sample',client)
    assert len(rows) == 2 and len(manifest['conflicts']) == 1
    assert '.debug' not in next(r['link'] for r in rows if '.tgz' in r['link'])


@pytest.fixture
def db(tmp_path):
    db = D1Client(local_sqlite_path=str(tmp_path/'audit.db'))
    db.execute_script(Path(__file__).parents[1].joinpath('schema.sql').read_text())
    ensure_base_brands_and_anchor(db)
    yield db
    db.close()


def test_scheduler_scope_and_round_robin_never_select_unplanned_rows(db):
    planned = [{'id':i,'public_token':str(i),'device_codename':c,'extract_status':'pending','version_str':'V1'} for i,c in enumerate(['sample','second','sample','second'])]
    scoped = ScopedDB(db,planned,['sample','second'])
    selected = scoped.query("SELECT * FROM stock_firmwares WHERE extract_status='pending' AND extract_retries<3 ORDER BY id LIMIT ?", (2,))
    assert [r['id'] for r in selected] == [0,1]
    batch = choose_batch(planned,{'0':{'final':True}},['sample','second'])
    assert [r['id'] for r in batch] == [2,1,3]


def test_empty_finished_run_is_a_failed_coverage_report(db, monkeypatch):
    monkeypatch.delenv('GITHUB_STEP_SUMMARY',raising=False)
    control = {'run_id':'test_run','state':'finished','generation':0,'tokens':[],
               'targets':[{'codename':'sample','name':'Sample'}]}
    save(db,'test_run:source:sample',{'discovered_pages':0,'errors':[{'error':'NoHistoryFound'}]})
    result = report(db,control)
    assert result['all_requested_coverage_passed'] is False
    assert result['devices'][0]['complete'] is False


def test_live_main_refuses_local_execution_before_any_network(monkeypatch):
    monkeypatch.delenv('GITHUB_ACTIONS',raising=False)
    assert main([]) == 1


def test_continuation_dispatch_pins_the_ref_and_generation(monkeypatch):
    import json
    from fmr_extractor import history_sweep
    seen = []
    original = httpx.Client
    def handler(request):
        seen.append(request)
        return httpx.Response(204)
    monkeypatch.setenv('GITHUB_REPOSITORY','owner/runner')
    monkeypatch.setenv('GH_WORKFLOW_TOKEN','ephemeral-test-only')
    monkeypatch.setattr(history_sweep.httpx, 'Client', lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    history_sweep.dispatch({'ref':'frozen-test-tag','run_id':'test_run','generation':3})
    assert len(seen) == 1
    assert seen[0].url.host == 'api.github.com'
    assert json.loads(seen[0].content) == {'ref':'frozen-test-tag', 'inputs':{'run_id':'test_run','generation':'3'}}


def test_live_main_refuses_a_mutable_branch(monkeypatch):
    monkeypatch.setenv('GITHUB_ACTIONS','true')
    monkeypatch.setenv('GITHUB_REF_TYPE','branch')
    assert main([]) == 1


def test_transient_link_status_is_retried_but_no_full_body_is_read(monkeypatch):
    from fmr_extractor.history_sweep import checked_link
    monkeypatch.setenv('FMR_TEST_NO_SLEEP','1')
    calls = []
    class Forbidden(httpx.SyncByteStream):
        def __iter__(self):
            raise AssertionError('Full body consumed')
            yield b''
    def handler(request):
        calls.append(request)
        return httpx.Response(503 if len(calls) < 3 else 200, stream=Forbidden())
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = checked_link(client,'https://bigota.d.miui.com/test.zip')
    assert result == {'status':200,'live':True,'range':False} and len(calls) == 3


def test_pending_catalog_rows_do_not_become_a_passing_report(db,monkeypatch):
    monkeypatch.delenv('GITHUB_STEP_SUMMARY',raising=False)
    db.execute("INSERT INTO devices(codename,brand_id,marketing_name) VALUES ('sample','other','Sample')")
    db.execute("INSERT INTO stock_firmwares(public_token,device_codename,region_code,version_str,android_version,package_type,original_official_url,fast_cdn_url,ed25519_signature) VALUES ('fw_pending00001','sample','CN','V1','13','recovery_zip','https://example.com/sample.zip','https://example.com/sample.zip',?)",('0'*128,))
    control={'run_id':'test_run','state':'working','generation':0,'tokens':['fw_pending00001'],'targets':[{'codename':'sample','name':'Sample'}]}
    save(db,'test_run:source:sample',{'discovered_pages':1,'errors':[],'conflicts':[]})
    result=report(db,control)
    assert result['devices'][0]['unfinished'] == 1
    assert result['devices'][0]['missing_image_versions'] == 1
    assert result['all_requested_coverage_passed'] is False
