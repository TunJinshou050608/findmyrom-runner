import json
from urllib.parse import parse_qs,urlsplit
import httpx
import pytest
from fmr_tg_scanner.channel_search import search_page
from fmr_tg_scanner.message_reader import read_message,MessageIdentityError
from fmr_tg_scanner.search_http import SearchHTTP,metadata_url,WorkBudgetReached,SearchTransportError
from fmr_tg_scanner.search_store import SearchStore
from fmr_tg_scanner.search_policy import analyse
from fmr_tg_scanner.search_discovery import indexed_messages
from fmr_tg_scanner.html_parser import parse_tg_channel_page


def page(ids=(12,11),more='',text='#alioth Evolution X unofficial Android 15'):
    return '<section class="tgme_widget_history">'+''.join(
        f'<div class="tgme_widget_message" data-post="testchannel/{i}"><div class="tgme_widget_message_text">{text} <a href="https://sourceforge.net/project/files/alioth-{i}.zip">Download</a></div></div>' for i in ids)+more+'</section>'


@pytest.mark.parametrize('url',[
    'https://t.me/s/testchannel?q=POCO+F3&before=10',
    'https://t.me/testchannel/10?embed=1&mode=tme','https://t.me/testchannel'])
def test_metadata_routes(url):assert metadata_url(url)==url


@pytest.mark.parametrize('url',[
    'http://t.me/s/testchannel','https://127.0.0.1/s/testchannel',
    'https://t.me.evil.com/s/testchannel','https://evil@t.me/s/testchannel',
    'https://t.me/s/testchannel?before=-1','https://t.me/s/testchannel?q=x&q=y',
    'https://t.me/file.zip','https://t.me/testchannel/10',
    'https://api.github.com/repos/owner/repo/contents/file.zip'])
def test_nonmetadata_routes_rejected(url):
    with pytest.raises(ValueError):metadata_url(url)


def test_search_preserves_query_and_detects_incomplete_pagination():
    seen=[]
    def fetch(url):
        seen.append(url)
        return page(more='<a class="tme_messages_more" href="/s/testchannel?q=POCO+F3&amp;before=11">More</a>')
    result=search_page(fetch,'testchannel','POCO F3')
    assert result['has_more'] and result['state']['before']==11
    repeated=search_page(fetch,'testchannel','POCO F3',result['state'])
    assert parse_qs(urlsplit(seen[-1]).query)=={'q':['POCO F3'],'before':['11']}
    assert repeated['status']=='repeated_search_page' and not repeated['has_more']
    lost=search_page(lambda _:page(more='<a class="tme_messages_more" href="/s/testchannel?before=11">More</a>'),'testchannel','alioth')
    assert lost['status']=='pagination_lost_query'


def test_exact_floor_verification():
    msg,url=read_message(lambda _:page(),'testchannel',11)
    assert msg['msg_id']==11 and '/11?' in url
    with pytest.raises(MessageIdentityError):read_message(lambda _:page(),'testchannel',99)


def test_index_retains_multiple_floors_and_rejects_lookalike_hosts():
    messages,channels=indexed_messages('<a href="https://t.me/testchannel/1">a</a><a href="https://t.me/s/testchannel/2">b</a><a href="https://t.me/testchannel">c</a><a href="https://t.me.evil.com/testchannel/3">d</a>')
    assert [m['msg_id'] for m in messages]==[1,2] and channels==[{'channel':'testchannel'}]


def test_fair_queue_and_interrupted_work_recovery(tmp_path):
    path=tmp_path/'state.sqlite';store=SearchStore(path)
    store.enqueue('query','alioth',{'channel':'testchannel','state':{}},1)
    store.enqueue('index','ishtar',{'query':'ishtar'},40)
    first=store.due(['alioth','ishtar']);assert first['device']=='alioth'
    store.close();store=SearchStore(path)
    second=store.due(['alioth','ishtar']);assert second['device']=='ishtar'
    store.finish(second,delay=3600)
    assert store.due(['alioth','ishtar'])['key']==first['key'];store.close()


def test_shared_cache_request_budget_and_retry(tmp_path,monkeypatch):
    monkeypatch.setenv('FMR_TEST_NO_SLEEP','1');db=SearchStore(tmp_path/'state.sqlite');calls=[]
    def handler(req):
        calls.append(str(req.url))
        return httpx.Response(429 if len(calls)==1 else 200,headers={'content-type':'text/html','retry-after':'0'},text='metadata')
    client=httpx.Client(transport=httpx.MockTransport(handler))
    http=SearchHTTP(db,lambda _:None,max_requests=2,client=client)
    assert http.get('https://t.me/s/testchannel')=='metadata'
    assert http.get('https://t.me/s/testchannel')=='metadata'
    assert http.requests==2 and http.cache_hits==1 and http.errors==1
    with pytest.raises(WorkBudgetReached):http.get('https://t.me/s/otherchannel')
    client.close();db.close()


def test_binary_content_rejected(tmp_path):
    db=SearchStore(tmp_path/'state.sqlite')
    client=httpx.Client(transport=httpx.MockTransport(lambda _:httpx.Response(200,headers={'content-type':'application/zip'},content=b'zip')))
    http=SearchHTTP(db,lambda _:None,client=client)
    with pytest.raises(SearchTransportError):http.get('https://t.me/s/testchannel')
    assert http.bytes==0;client.close();db.close()


def test_device_proof_stock_filter_and_unknown_authorship(tmp_path):
    msg=parse_tg_channel_page(page(),'testchannel')['messages'][0]
    result,reason=analyse(msg,'alioth',['POCO F3'],{'alioth':['POCO F3'],'ishtar':['Xiaomi 13 Ultra']})
    assert reason=='accepted' and result['package']['origin_type']=='unknown'
    assert result['package']['maintainer_handle'] is None and result['package']['sender_id'] is None
    wrong=dict(msg,text='#ishtar Evolution X unofficial Android 15')
    assert analyse(wrong,'alioth',[],{'alioth':[],'ishtar':[]})[0] is None
    official=dict(msg,text='#alioth Evolution X Official Android 15')
    assert analyse(official,'alioth',[],{'alioth':[]})[1]=='excluded_mainstream_official_rom'
    recovery=dict(msg,text='#alioth TWRP Official recovery Android 15')
    assert analyse(recovery,'alioth',[],{'alioth':[]})[1]=='accepted'
    db=SearchStore(tmp_path/'state.sqlite');pkg=result['package']
    assert db.save(pkg,result['evidence'],'unofficial','accepted',True)
    assert not db.save(pkg,result['evidence'],'unofficial','accepted',True)
    assert len(db.query('SELECT * FROM tg_search_records'))==1;db.close()


def test_cold_discovery_produces_resource_and_restart_reuses_state(tmp_path,monkeypatch):
    from fmr_tg_scanner.search_engine import run
    monkeypatch.setenv('FMR_TEST_NO_SLEEP','1');requests=[];phase=[0]
    def response(req):
        requests.append(str(req.url))
        if req.url.host=='raw.githubusercontent.com':body='alioth: ["POCO F3"]'
        elif req.url.host=='api.github.com':body='{"items":[]}'
        elif req.url.host=='lyzem.com':body='<html></html>'
        elif req.url.path=='/s/aliothupdates':
            ids=(28,12) if req.url.params.get('before')=='29' else (30,29) if phase[0] else (12,11)
            body='<h1 class="tgme_channel_info_header_title">POCO F3 Updates</h1>'+page(ids).replace('testchannel/','aliothupdates/')
        else:body='<html><div class="tgme_page_title">Contact</div></html>'
        return httpx.Response(200,headers={'content-type':'text/html'},text=body)
    def factory(*args,**kwargs):return SearchHTTP(*args,**kwargs,client=httpx.Client(transport=httpx.MockTransport(response)))
    first=run(tmp_path,['alioth'],max_requests=40,max_seconds=30,http_factory=factory)
    assert first['new_qualified_products']==2 and first['packages']==2
    assert first['D1_requests']==0 and first['llm_calls']==0
    assert all('sourceforge.net' not in url for url in requests)
    db=SearchStore(tmp_path/'search.sqlite')
    assert db.query("SELECT kind FROM tg_search_tasks WHERE kind='history'")
    assert not db.query("SELECT kind FROM tg_search_tasks WHERE kind='query'");db.close()
    before=len(requests)
    second=run(tmp_path,['alioth'],max_requests=40,max_seconds=30,http_factory=factory)
    assert second['new_qualified_products']==0 and second['packages']==2
    assert len(requests)==before and second['http_requests']==0
    phase[0]=1;db=SearchStore(tmp_path/'search.sqlite')
    db.execute('DELETE FROM tg_http_cache')
    db.execute("UPDATE tg_search_tasks SET next_at=0 WHERE kind='head'")
    db.close()
    third=run(tmp_path,['alioth'],max_requests=40,max_seconds=30,http_factory=factory)
    assert third['new_qualified_products']==3 and third['packages']==5
    assert any('before=29' in url for url in requests)
    db=SearchStore(tmp_path/'search.sqlite')
    state=json.loads(db.query("SELECT payload FROM tg_search_tasks WHERE kind='head'")[0]['payload'])['state']
    assert state['anchor']==30 and 'before' not in state;db.close()


@pytest.mark.parametrize('text,url,reason',[
    ('#liuqin New OS Fastboot Update Available for Xiaomi Pad 6 Pro','https://cdn-ota.azureedge.net/OS1.0/liuqin_images_OS1.0.4.0.UMYCNXM.tgz','excluded_stock_firmware'),
    ('#liuqin New OS Recovery Update','https://cdn-ota.azureedge.net/miui_LIUQIN_OS1.0.9.0.UMYCNXM.zip','excluded_stock_firmware'),
    ('#liuqin HentaiOS GSI port Android 12 from Xiaomi Pad 6 Pro','https://sourceforge.net/files/HentaiOS-AB-12-ErfanGSI.img.gz','generic_image_donor_is_not_target'),
    ('#liuqin SECURITY WARNING Android scam package','https://download.example.com/suspicious.zip','not_a_release')])
def test_live_trial_false_positives_become_regressions(text,url,reason):
    msg=parse_tg_channel_page(page(text=text).replace('https://sourceforge.net/project/files/alioth-12.zip',url),'testchannel')['messages'][0]
    result,actual=analyse(msg,'liuqin',[],{'liuqin':[]})
    assert actual==reason and (result is None or not result['accepted'])


def test_multi_device_profile_does_not_prove_target():
    msg=parse_tg_channel_page(page(text='Android custom ROM release').replace('alioth-12.zip','build-12.zip'),'testchannel')['messages'][0]
    assert analyse(msg,'alioth',[],{'alioth':[],'umi':[]},'Alioth (DEAD) Umi (Discontinued)')[0] is None


def test_rom_heading_wins_over_companion_instructions():
    text='#A16 #OxygenOS #Alioth #Port<br>OxygenOS v16 Android 16<br>Install with TWRP recovery and kernel'
    msg=parse_tg_channel_page(page(text=text),'testchannel')['messages'][0]
    result,reason=analyse(msg,'alioth',[],{'alioth':[]})
    assert reason=='accepted' and result['package']['resource_kind']=='rom' and result['package']['project_name']=='OxygenOS'


def test_mainstream_status_unknown_is_candidate_until_evidence():
    msg=parse_tg_channel_page(page(text='#alioth LineageOS Android 16'),'testchannel')['messages'][0]
    result,reason=analyse(msg,'alioth',[],{'alioth':[]})
    assert reason=='mainstream_release_status_unproved' and not result['accepted']
    msg['text']='#alioth LineageOS Community Android 16'
    assert analyse(msg,'alioth',[],{'alioth':[]})[1]=='accepted'


def test_official_rom_scope_and_port_kind():
    msg=parse_tg_channel_page(page(text='#alioth HyperOS Android 15<br>ported from another Xiaomi model'),'testchannel')['messages'][0]
    result,reason=analyse(msg,'alioth',[],{'alioth':[]})
    assert reason=='accepted' and result['package']['resource_kind']=='rom'
    msg['text']='#alioth UnlistedROM Official Android 15 ROM'
    assert analyse(msg,'alioth',[],{'alioth':[]})[1]=='official_rom_scope_unproved'
    msg['text']='#alioth Bliss ROM 16 Android 13\nFollow Bliss Official Channel'
    assert analyse(msg,'alioth',[],{'alioth':[]})[1]=='excluded_mainstream_official_rom'


def test_resident_reuses_discovered_sources_without_importing_products(tmp_path,monkeypatch):
    from fmr_tg_scanner import search_watch
    from fmr_tg_scanner.trial import atomic_json
    path=tmp_path/'experiments/search-v3/123/query_first/products.json'
    atomic_json(path,{'products':[{'package':{'device_codename':'annibale','channel_username':'autofoundupdates','tg_post_url':'https://t.me/autofoundupdates/12'}}]})
    def run(root,targets,**_):
        db=SearchStore(root/'search.sqlite')
        assert not db.query('SELECT * FROM tg_search_records')
        task=db.query('SELECT * FROM tg_search_tasks')[0]
        assert task['kind']=='admit' and json.loads(task['payload'])=={'channel':'autofoundupdates','depth':0}
        assert db.meta('prior_lab_source_evidence')[0]['evidence_post']=='https://t.me/autofoundupdates/12'
        db.close();return {'ok':True,'packages':0}
    monkeypatch.setattr(search_watch,'run',run)
    assert search_watch.produce(tmp_path)['packages']==0


@pytest.mark.parametrize('heading,kind',[
    ('#Kernel Official #alioth<br>Kernel for LineageOS Android 15','kernel'),
    ('#TWRP Official #alioth<br>Recovery for LineageOS Android 15','recovery')])
def test_official_companion_resource_survives_rom_mentions(heading,kind):
    msg=parse_tg_channel_page(page(text=heading),'testchannel')['messages'][0]
    result,reason=analyse(msg,'alioth',[],{'alioth':[]})
    assert reason=='accepted' and result['package']['resource_kind']==kind


def test_multidevice_announcement_delivers_its_message_for_each_declared_model():
    html=page(text='#alioth #liuqin Evolution X unofficial Android 15')
    html=html.replace('Download</a>','Download</a><a href="https://sourceforge.net/files/EvolutionX_liuqin-15.zip">Download</a>')
    html=html.replace('project/files/alioth-12.zip','files/EvolutionX_alioth-15.zip')
    msg=parse_tg_channel_page(html,'testchannel')['messages'][0]
    result,reason=analyse(msg,'alioth',[],{'alioth':[],'liuqin':[]})
    assert reason=='accepted'
    assert json.loads(result['package']['download_links_json'])==[]
    assert result['package']['target_post_url']=='https://t.me/testchannel/12'
    assert result['package']['delivery_type']=='telegram_message'


def test_shared_aggregator_reaches_model_without_own_channel(tmp_path,monkeypatch):
    from fmr_tg_scanner.search_engine import run
    monkeypatch.setenv('FMR_TEST_NO_SLEEP','1');requests=[]
    def response(req):
        requests.append(str(req.url))
        if req.url.host=='raw.githubusercontent.com':body='alioth: ["POCO F3"]\nishtar: ["Xiaomi 13 Ultra"]'
        elif req.url.host=='api.github.com':body='{"items":[]}'
        elif req.url.host=='lyzem.com':body='<html></html>'
        elif req.url.path=='/s/aliothupdates':
            code='ishtar' if req.url.params.get('q')=='ishtar' else 'alioth'
            body='<h1 class="tgme_channel_info_header_title">Android Custom ROM Updates</h1>'+page(ids=(13,),text=f'#{code} Evolution X unofficial Android 15').replace('testchannel/','aliothupdates/').replace('alioth-13.zip',code+'-13.zip')
        else:body='<html></html>'
        return httpx.Response(200,headers={'content-type':'text/html'},text=body)
    def factory(*args,**kwargs):return SearchHTTP(*args,**kwargs,client=httpx.Client(transport=httpx.MockTransport(response)))
    result=run(tmp_path,['alioth','ishtar'],max_requests=90,max_seconds=30,http_factory=factory)
    assert result['per_device']['ishtar']==1 and result['coverage_ok']
    assert any('/s/aliothupdates?q=ishtar' in url for url in requests)


def test_telegram_metadata_normalizes_bare_links_and_explicit_handles():
    from fmr_tg_scanner.search_discovery import text_telegram_links
    messages,channels=text_telegram_links('Telegram: @romupdates; t.me/releases_channel/123; tg://resolve?domain=anotherchannel; thanks @github_contributor')
    assert messages[0]['msg_id']==123
    assert {c['channel'] for c in channels}=={'romupdates','anotherchannel'}


def test_release_metadata_discovers_target_when_readme_missing(tmp_path,monkeypatch):
    from fmr_tg_scanner.search_engine import run
    monkeypatch.setenv('FMR_TEST_NO_SLEEP','1');requests=[]
    def response(req):
        requests.append(str(req.url));status=200;kind='text/html'
        if req.url.host=='raw.githubusercontent.com':body='liuqin: ["Xiaomi Pad 6 Pro"]'
        elif req.url.path=='/search/repositories':
            body=json.dumps({'items':[{'full_name':'dev/ROM-liuqin','description':'Custom ROM for Xiaomi Pad 6 Pro'}]});kind='application/json'
        elif req.url.host=='api.github.com':
            kind='application/json'
            if req.url.path.endswith('/readme'):body='{}';status=404
            elif req.url.path.endswith('/releases'):body=json.dumps([{'body':'Announcement t.me/releaseupdates/123'}])
            else:body='{}'
        elif req.url.path=='/releaseupdates/123':body=page(ids=(123,),text='#liuqin Lunaris unofficial Android 15').replace('testchannel/','releaseupdates/').replace('alioth-123.zip','liuqin-123.zip')
        else:body='<html></html>'
        return httpx.Response(status,headers={'content-type':kind},text=body)
    def factory(*args,**kwargs):return SearchHTTP(*args,**kwargs,client=httpx.Client(transport=httpx.MockTransport(response)))
    result=run(tmp_path,['liuqin'],max_requests=70,max_seconds=30,http_factory=factory)
    assert result['per_device']=={'liuqin':1}
    assert result['http_errors']>=1
    assert any('/releases?' in url for url in requests)


def test_port_donor_model_does_not_become_a_target():
    msg=parse_tg_channel_page(page(text='Custom ROM for POCO F3<br>Ported from Xiaomi 13 Ultra (ishtar)'), 'testchannel')['messages'][0]
    # Parentheses in a donor declaration are not an explicit target label.
    assert analyse(msg,'ishtar',['Xiaomi 13 Ultra'],{'ishtar':['Xiaomi 13 Ultra'],'alioth':['POCO F3']})[0] is None


def test_private_checkpoint_rebases_two_disjoint_writers(tmp_path,monkeypatch):
    import subprocess
    from fmr_tg_scanner.watch import publish_private
    monkeypatch.setenv('GIT_CONFIG_GLOBAL',str(tmp_path/'empty-config'))
    def git(*args):return subprocess.run(['git',*map(str,args)],check=True,capture_output=True,text=True)
    remote=tmp_path/'remote.git';first=tmp_path/'first';second=tmp_path/'second'
    git('init','--bare','--initial-branch=main',remote)
    git('clone',remote,first)
    (first/'base.json').write_text('{}')
    # The empty remote has no main to pull until its first commit exists.
    git('-C',first,'add','base.json')
    git('-C',first,'-c','user.name=Test','-c','user.email=test@example.invalid','commit','-m','base')
    git('-C',first,'push','origin','HEAD:main')
    git('clone',remote,second)
    (first/'resident.json').write_text('{"ok":true}')
    assert publish_private(first)
    (second/'experiment.json').write_text('{"ok":true}')
    assert publish_private(second)
    assert (second/'resident.json').exists()
    assert git('-C',second,'show','origin/main:experiment.json').stdout=='{"ok":true}'


def test_alias_device_proof_counts_for_opaque_cloud_package(tmp_path):
    from fmr_tg_scanner.search_discovery import aliases,query_aliases
    db=SearchStore(tmp_path/'state.sqlite');db.set_meta('device_aliases_v3',{'liuqin':['Xiaomi Pad 6 Pro']})
    names=aliases(None,db,['liuqin'])
    assert '小米平板6Pro' in query_aliases(names['liuqin'])
    msg=parse_tg_channel_page(page(text='Lunaris unofficial Android 15 for 小米平板6Pro').replace('https://sourceforge.net/project/files/alioth-12.zip','https://drive.google.com/file/d/opaque-id/view'),'testchannel')['messages'][0]
    result,reason=analyse(msg,'liuqin',names['liuqin'],names)
    assert reason=='accepted' and result['package']['rule_confidence']>=.8;db.close()


def test_exact_floor_reader_uses_public_preview_when_embed_has_no_message():
    urls=[]
    def fetch(url):
        urls.append(url)
        return '<html>Message unavailable in embed</html>' if 'embed=1' in url else page(ids=(12,))
    msg,url=read_message(fetch,'testchannel',12)
    assert msg['msg_id']==12 and url=='https://t.me/s/testchannel?before=13'
    assert len(urls)==2


def test_reference_only_post_is_followed_to_actual_package(tmp_path,monkeypatch):
    from fmr_tg_scanner.search_engine import run
    monkeypatch.setenv('FMR_TEST_NO_SLEEP','1')
    def response(req):
        if req.url.host=='raw.githubusercontent.com':body='alioth: ["POCO F3"]'
        elif req.url.host=='api.github.com':body='{"items":[]}'
        elif req.url.path=='/s/aliothupdates':body=page(ids=(1,),text='#alioth ROM release is here').replace('testchannel/','aliothupdates/').replace('https://sourceforge.net/project/files/alioth-1.zip','https://t.me/actualreleases/123')
        elif req.url.path=='/actualreleases/123':body=page(ids=(123,),text='#alioth Lunaris unofficial Android 15').replace('testchannel/','actualreleases/')
        else:body='<html></html>'
        return httpx.Response(200,headers={'content-type':'text/html'},text=body)
    def factory(*args,**kwargs):return SearchHTTP(*args,**kwargs,client=httpx.Client(transport=httpx.MockTransport(response)))
    result=run(tmp_path,['alioth'],max_requests=80,max_seconds=30,http_factory=factory)
    assert result['per_device']=={'alioth':2}
    saved=json.loads((tmp_path/'products.json').read_text())['products']
    assert {p['package']['tg_post_url'] for p in saved}=={'https://t.me/actualreleases/123','https://t.me/aliothupdates/1'}


def test_release_delivers_message_without_file_links_or_attachment():
    html=page(text='#ishtar AviumUI unofficial ROM Android 16 release<br>Updated: 2026/07/18<br>Download and support in this post')
    html=html.replace('<a href="https://sourceforge.net/project/files/alioth-12.zip">Download</a>','')
    msg=parse_tg_channel_page(html,'testchannel')['messages'][0]
    assert not msg['links'] and not msg['has_tg_attachment']
    result,reason=analyse(msg,'ishtar',['Xiaomi 13 Ultra'],{'ishtar':['Xiaomi 13 Ultra']})
    assert reason=='accepted'
    assert result['package']['target_post_url']=='https://t.me/testchannel/12'
    assert result['package']['download_links_json']=='[]'


def test_channel_name_candidates_cover_generic_singular_and_plural_updates():
    from fmr_tg_scanner.search_discovery import name_candidates
    candidates=name_candidates('fictional',['Example 42'])
    assert 'example42update' in candidates and 'example42updates' in candidates


def test_higher_priority_discovery_revives_once_without_erasing_cooldown(tmp_path):
    db=SearchStore(tmp_path/'state.sqlite');payload={'channel':'exampleupdates','depth':0}
    db.enqueue('admit','example',payload,50);task=db.due(['example']);db.finish(task,delay=86400)
    db.enqueue('admit','example',payload,3)
    promoted=db.due(['example']);assert promoted and promoted['priority']==3
    db.finish(promoted,delay=86400);db.enqueue('admit','example',payload,3)
    assert db.due(['example']) is None;db.close()


def test_short_telegram_release_has_no_file_or_minimum_caption_gate():
    msg=parse_tg_channel_page(page(text='#ishtar kernel updated'),'testchannel')['messages'][0]
    msg['links']=[];msg['link_items']=[]
    result,reason=analyse(msg,'ishtar',[],{'ishtar':[]})
    assert reason=='accepted' and result['package']['target_post_url']=='https://t.me/testchannel/12'


def test_attachment_only_post_still_delivers_the_telegram_floor():
    msg=parse_tg_channel_page(page(),'testchannel')['messages'][0]
    msg.update(text='',links=[],link_items=[],has_tg_attachment=True,file_attachment_name='lineage-23.2-UNOFFICIAL-liuqin.zip')
    result,reason=analyse(msg,'liuqin',[],{'liuqin':[]})
    assert reason=='accepted' and result['package']['target_post_url']=='https://t.me/testchannel/12'


def test_longer_model_names_do_not_make_dedicated_channel_ambiguous():
    from fmr_tg_scanner.search_policy import profile_devices
    names={'pipa':['Xiaomi Pad 6'],'liuqin':['Xiaomi Pad 6 Pro'],'fuxi':['Xiaomi 13'],'ishtar':['Xiaomi 13 Ultra']}
    assert profile_devices('Xiaomi Pad 6 Pro Updates',names)=={'liuqin'}
    assert profile_devices('Xiaomi 13 Ultra Updates',names)=={'ishtar'}
    assert profile_devices('Xiaomi Pad 6 Pro / Xiaomi Pad 6 Updates',names)=={'liuqin','pipa'}
    msg=parse_tg_channel_page(page(text='TWRP recovery release'),'testchannel')['messages'][0]
    msg['links']=[];msg['link_items']=[]
    assert analyse(msg,'liuqin',names['liuqin'],names,'Xiaomi Pad 6 Pro Updates')[1]=='accepted'
    assert analyse(msg,'pipa',names['pipa'],names,'Xiaomi Pad 6 Pro Updates')[0] is None
    msg['text']='TWRP recovery released for Xiaomi Pad 6'
    assert analyse(msg,'liuqin',names['liuqin'],names,'Xiaomi Pad 6 Pro Updates')[0] is None


def test_paranoid_android_release_without_file_link_uses_dedicated_profile():
    msg=parse_tg_channel_page(page(text='Paranoid Android Topaz Unofficial<br>Download<br>How to flash'),'testchannel')['messages'][0]
    msg['links']=[];msg['link_items']=[]
    names={'liuqin':['Xiaomi Pad 6 Pro'],'pipa':['Xiaomi Pad 6']}
    result,reason=analyse(msg,'liuqin',names['liuqin'],names,'Xiaomi Pad 6 Pro | Updates')
    assert reason=='accepted' and result['package']['project_name']=='Paranoid Android'
    assert result['package']['target_post_url']=='https://t.me/testchannel/12'


def test_community_official_roms_accepted():
    for proj in ['DerpFest', 'AxionOS', 'RisingOS', 'VoltageOS']:
        msg = parse_tg_channel_page(page(text=f'#{proj} Official Android 15 #alioth\nDownload from SourceForge'), 'testchannel')['messages'][0]
        result, reason = analyse(msg, 'alioth', [], {'alioth': []})
        assert reason == 'accepted'
        assert result['classification'] == 'community_official'
        assert result['package']['resource_kind'] == 'rom'


def test_mainstream_official_roms_excluded():
    for proj in ['Evolution X', 'crDroid', 'PixelOS', 'LineageOS']:
        msg = parse_tg_channel_page(page(text=f'#{proj} Official Android 15 #alioth\nDownload from SourceForge'), 'testchannel')['messages'][0]
        result, reason = analyse(msg, 'alioth', [], {'alioth': []})
        assert reason == 'excluded_mainstream_official_rom'
        assert result['classification'] == 'mainstream_official'


def test_mainstream_unofficial_roms_accepted():
    for proj in ['Evolution X', 'crDroid', 'PixelOS', 'LineageOS']:
        msg = parse_tg_channel_page(page(text=f'#{proj} Unofficial Android 15 #alioth\nDownload from SourceForge'), 'testchannel')['messages'][0]
        result, reason = analyse(msg, 'alioth', [], {'alioth': []})
        assert reason == 'accepted'
        assert result['classification'] == 'unofficial'


def test_aviumui_notes_stripping():
    text = """AviumUI-16.2.1 | Community | Android 16 | POCO F8 PRO [ANNIBALE]
Updated: 25/06/'26
Notes:
• Flash using twrp or which recovery you want use
Download: https://sourceforge.net/project/files/avium.zip"""
    msg = parse_tg_channel_page(page(text=text), 'testchannel')['messages'][0]
    result, reason = analyse(msg, 'annibale', [], {'annibale': []})
    assert reason == 'accepted'
    assert result['package']['resource_kind'] == 'rom'
    assert result['package']['project_name'] == 'AviumUI'


def test_awakenos_credits_stripping():
    text = """AwakenOS v3.0 | Community | Android 14 [ALIOTH]
Updated: 10/10/'24
Credits:
• Thanks to @dev for kernel source
Download: https://sourceforge.net/project/files/awaken.zip"""
    msg = parse_tg_channel_page(page(text=text), 'testchannel')['messages'][0]
    result, reason = analyse(msg, 'alioth', [], {'alioth': []})
    assert reason == 'accepted'
    assert result['package']['resource_kind'] == 'rom'
    assert result['package']['project_name'] == 'AwakenOS'


def test_non_release_posts_rejected():
    cases = [
        'Xiaomi smartphones offer the Worst Charging Solution\nFailing to boot after battery goes to 0% update',
        'We have important news to share!\nOur custom ROM list has been evolving over the months',
        'From now on use skk recovery latest one for all my roms.',
        'MiuiCamera Leica Port (Magisk Module)\nTested on alioth with magisk update',
    ]
    for text in cases:
        msg = parse_tg_channel_page(page(text=text), 'testchannel')['messages'][0]
        result, reason = analyse(msg, 'alioth', [], {'alioth': []})
        assert reason == 'not_a_release' and result is None


def test_screenshot_channel_rejected():
    html = page(text='PixelOS - UNOFFICIAL\nBy @dev\nDownload: https://sourceforge.net/test.zip').replace('testchannel/', 'aliothscreenshots/')
    msg = parse_tg_channel_page(html, 'aliothscreenshots')['messages'][0]
    result, reason = analyse(msg, 'alioth', [], {'alioth': []})
    assert reason == 'not_a_release' and result is None


def test_cross_device_topaz_rejected_for_liuqin():
    html = page(text='Paranoid Android Topaz Unofficial\nDownload\nHow to flash').replace('testchannel/', 'xiaomipad6proupdates/')
    msg = parse_tg_channel_page(html, 'xiaomipad6proupdates')['messages'][0]
    msg['links'] = []
    msg['link_items'] = []
    names = {'liuqin': ['Xiaomi Pad 6 Pro'], 'topaz': ['Redmi Note 12 4G']}
    result, reason = analyse(msg, 'liuqin', names['liuqin'], names, 'Xiaomi Pad 6 Pro | Updates\nOfficial updates channel for Xiaomi Pad 6 Pro (liuqin)')
    assert result is None and reason == 'wrong_or_unproved_device'


def test_hashtag_heading_skips_indexing_tags_to_extract_clean_title():
    text = """#CherishOS #Official #T #ROM #Alioth
CherishOS v4.10 - OFFICIAL | Android 13
Updated: 01/08/'23
Download: https://sourceforge.net/files/download"""
    msg = parse_tg_channel_page(page(text=text).replace('alioth-12.zip', 'download'), 'testchannel')['messages'][0]
    result, reason = analyse(msg, 'alioth', [], {'alioth': []})
    assert reason == 'accepted'
    pkg = result['package']
    assert not pkg['package_name'].startswith('#')
    assert 'CherishOS v4.10 - OFFICIAL | Android 13' in pkg['package_name']
    assert pkg['project_name'] == 'CherishOS'
    assert pkg['android_version'] == '13'
    assert pkg['build_version_tag'] == 'v4.10'


def test_letter_codename_android_versions_detected():
    cases = [
        ('#LunarisAOSP #Unofficial #T #alioth\nLunaris 3.0\nDownload: https://sourceforge.net/rom.zip', '13'),
        ('#VoltageOS #U #alioth\nVoltage 3.2\nDownload: https://sourceforge.net/rom.zip', '14'),
        ('#RisingOS #V #alioth\nRising 5.1\nDownload: https://sourceforge.net/rom.zip', '15'),
        ('#AxionOS #Baklava #annibale\nAxion 2.9\nDownload: https://sourceforge.net/rom.zip', '16'),
    ]
    for text, expected_ver in cases:
        msg = parse_tg_channel_page(page(text=text), 'testchannel')['messages'][0]
        result, _ = analyse(msg, 'alioth' if 'alioth' in text else 'annibale', [], {'alioth': [], 'annibale': []})
        assert result['package']['android_version'] == expected_ver


def test_project_version_and_maintainer_without_at_sign():
    text = """LunarisAOSP 3.12 | Community | Android 16 | POCO F8 PRO [ANNIBALE]
Updated: 12/07/'26
🔸Maintainer: SMGReborn
Download: https://sourceforge.net/annibale/lunaris.zip"""
    msg = parse_tg_channel_page(page(text=text), 'testchannel')['messages'][0]
    result, reason = analyse(msg, 'annibale', [], {'annibale': []})
    assert reason == 'accepted'
    pkg = result['package']
    assert pkg['build_version_tag'] == '3.12'
    assert pkg['maintainer_display_name'] == 'SMGReborn'
    assert pkg['maintainer_handle'] == '@smgreborn'


def test_kernel_not_misclassified_as_stock_miui():
    text = """#Kernel #Metallic #R #S #T #Miui #Aosp #Alioth
RealKing V11.2-Rise | Android 11/12/13
Updated: 02/05/'23
By @Rohail33
Download: https://www.pling.com/p/1858015/"""
    msg = parse_tg_channel_page(page(text=text).replace('alioth-12.zip', 'kernel.zip'), 'testchannel')['messages'][0]
    result, reason = analyse(msg, 'alioth', [], {'alioth': []})
    assert reason == 'accepted'
    pkg = result['package']
    assert pkg['resource_kind'] == 'kernel'
    assert pkg['project_name'] in ('RealKing', 'Kernel')
    assert pkg['maintainer_handle'] == '@rohail33'


def test_custom_miui_rom_accepted():
    text = """MIUIVN V14.0.6.0
Ported from Xiaomi 13 for Poco F3
Download: https://sourceforge.net/miuivn.zip"""
    msg = parse_tg_channel_page(page(text=text), 'testchannel')['messages'][0]
    result, reason = analyse(msg, 'alioth', ['POCO F3'], {'alioth': ['POCO F3']})
    assert reason == 'accepted'
    assert result['classification'] == 'custom_or_port'


def test_farewell_and_advisory_rejected():
    advisory = "If anyone faced max CPU freq 3.18Ghz not working on MYUI latest\nCurrently flash N0 Kernel\nDownload: https://sourceforge.net/kernel.zip"
    msg1 = parse_tg_channel_page(page(text=advisory), 'testchannel')['messages'][0]
    assert analyse(msg1, 'alioth', [], {'alioth': []})[1] == 'not_a_release'

    farewell = "It was a long journey. Recently I'm going with many difficulties... so it's time to say goodbye to alioth community."
    msg2 = parse_tg_channel_page(page(text=farewell), 'testchannel')['messages'][0]
    assert analyse(msg2, 'alioth', [], {'alioth': []})[1] == 'not_a_release'


def test_canonical_build_fingerprint_deduplication():
    from fmr_tg_scanner.search_store import SearchStore
    store = SearchStore(':memory:')
    p1 = {
        'device_codename': 'alioth',
        'resource_kind': 'rom',
        'project_name': 'DerpFest',
        'build_version_tag': '13 Tango',
        'package_name': 'DerpFest 13 Tango - OFFICIAL',
        'delivery_type': 'telegram_message',
        'tg_post_url': 'https://t.me/author_channel/10',
    }
    p2 = {
        'device_codename': 'alioth',
        'resource_kind': 'rom',
        'project_name': 'DerpFest',
        'build_version_tag': '13 Tango',
        'package_name': 'DerpFest 13 Tango - OFFICIAL',
        'delivery_type': 'telegram_message',
        'tg_post_url': 'https://t.me/repost_channel/999',
    }
    assert store.fingerprint(p1) == store.fingerprint(p2)


def test_llm_disambiguates_novel_community_official_rom():
    from fmr_tg_scanner.llm_fallback import merge
    text = """#SkylineUI #Official #ROM #T
SkylineUI - OFFICIAL | Android 13
Updated: 04/05/23'
▪️Download [1.7GB]
By @Yohan_Black
Download: https://drive.google.com/file/d/123/view"""
    msg = parse_tg_channel_page(page(text=text), 'testchannel')['messages'][0]
    
    # 1. Pure rules without LLM: unproved scope for novel ROM declaring official
    res_rule, reason_rule = analyse(msg, 'alioth', [], {'alioth': []})
    assert reason_rule == 'official_rom_scope_unproved'
    assert res_rule['accepted'] is False

    # 2. With LLM disambiguation: rescues community official ROM
    def mock_llm(t, **kwargs):
        return {
            'accepted': True,
            'classification': 'community_official',
            'project_name': 'SkylineUI',
            'android_version': '13',
            'maintainer_handle': '@Yohan_Black',
            'resource_kind': 'rom'
        }
    res_llm, reason_llm = analyse(msg, 'alioth', [], {'alioth': []}, llm_callable=mock_llm)
    assert reason_llm == 'accepted'
    assert res_llm['accepted'] is True
    assert res_llm['package']['project_name'] == 'SkylineUI'
    assert res_llm['package']['android_version'] == '13'
    assert res_llm['package']['maintainer_handle'] == '@yohan_black'


def test_llm_disambiguates_mainstream_unofficial_build():
    text = """PixelOS | Android 17
Device: Poco F8 Pro / Redmi K90 ( annibale )
Updated: 02/10/2026
By: @ramshell688
Download: https://pixeldrain.com/u/123
Changelog: Fixed Dolby Vision."""
    msg = parse_tg_channel_page(page(text=text), 'testchannel')['messages'][0]

    # Without LLM: mainstream status unproved
    res_rule, reason_rule = analyse(msg, 'annibale', [], {'annibale': []})
    assert reason_rule == 'mainstream_release_status_unproved'

    # With LLM: recognized as community unofficial build
    def mock_llm(t, **kwargs):
        return {
            'accepted': True,
            'classification': 'unofficial',
            'project_name': 'PixelOS',
            'android_version': '17',
            'maintainer_handle': '@ramshell688'
        }
    res_llm, reason_llm = analyse(msg, 'annibale', [], {'annibale': []}, llm_callable=mock_llm)
    assert reason_llm == 'accepted'
    assert res_llm['classification'] == 'unofficial'
    assert res_llm['package']['project_name'] == 'PixelOS'
    assert res_llm['package']['maintainer_handle'] == '@ramshell688'


def test_llm_rejects_and_never_modifies_immutable_provenance():
    from fmr_tg_scanner.llm_fallback import merge
    pkg = {
        'project_name': 'Custom ROM',
        'device_codename': 'alioth',
        'resource_kind': 'rom',
        'download_links_json': '["https://sourceforge.net/legit.zip"]',
        'tg_post_url': 'https://t.me/chan/10',
        'original_post_url': 'https://t.me/chan/10',
        'raw_text_hash': 'abcdef123456',
        'rule_confidence': 0.5,
        'maintainer_handle': None,
        'android_version': None
    }
    # LLM returns metadata plus malicious/hallucinated link modifications
    llm_output = {
        'project_name': 'AwakenOS',
        'android_version': '14',
        'maintainer_handle': '@dev_lead',
        'download_links_json': '["https://malicious.com/virus.zip"]',
        'raw_text_hash': 'hacked_hash',
        'tg_post_url': 'https://t.me/fake/999'
    }
    merged = merge(pkg, llm_output)
    # Allowed metadata gaps are filled
    assert merged['project_name'] == 'AwakenOS'
    assert merged['android_version'] == '14'
    assert merged['maintainer_handle'] == '@dev_lead'
    assert merged['extraction_method'] == 'rule_plus_llm'
    # Provenance and links remain strictly unchanged
    assert merged['download_links_json'] == '["https://sourceforge.net/legit.zip"]'
    assert merged['tg_post_url'] == 'https://t.me/chan/10'
    assert merged['raw_text_hash'] == 'abcdef123456'


def test_llm_grounding_guard_blocks_hallucinated_entities():
    from fmr_tg_scanner.llm_fallback import merge
    pkg = {
        'project_name': 'Custom ROM',
        'device_codename': 'alioth',
        'resource_kind': 'rom',
        'rule_confidence': 0.5,
        'maintainer_handle': None,
        'build_version_tag': None,
    }
    raw_post_text = "New SkyLine build for alioth is ready. Compiled by @real_builder. Download here."
    # LLM hallucinates an unmentioned project name, maintainer, and version tag
    hallucinated_output = {
        'project_name': 'ParanoidAndroid',  # Not in text
        'maintainer_handle': '@fake_imposter',  # Not in text
        'build_version_tag': 'v99.0-super',  # Not in text
    }
    merged = merge(pkg, hallucinated_output, text=raw_post_text)
    # Hallucinated values MUST be dropped because they cannot be grounded in the raw text
    assert merged['project_name'] == 'Custom ROM'  # Kept original, rejected ParanoidAndroid
    assert merged['maintainer_handle'] is None      # Dropped fake_imposter
    assert merged['build_version_tag'] is None      # Dropped v99.0

    # Legitimate values that ARE present in raw_post_text are accepted
    legit_output = {
        'project_name': 'SkyLine',
        'maintainer_handle': '@real_builder',
    }
    merged_legit = merge(pkg, legit_output, text=raw_post_text)
    assert merged_legit['project_name'] == 'SkyLine'
    assert merged_legit['maintainer_handle'] == '@real_builder'


def test_dynamic_project_learning_cache_prevents_redundant_llm_calls():
    from fmr_tg_scanner import search_policy
    search_policy.DYNAMIC_COMMUNITY_PROJECTS.clear()
    
    msg1_text = """#CloverProject #Official #ROM
CloverProject - OFFICIAL | Android 14
Updated: 01/01/24
By @clover_dev
Download: https://drive.google.com/file/d/111"""
    msg1 = parse_tg_channel_page(page(text=msg1_text), 'testchannel')['messages'][0]

    llm_calls = 0
    def mock_llm(t, **kwargs):
        nonlocal llm_calls
        llm_calls += 1
        return {
            'accepted': True,
            'classification': 'community_official',
            'project_name': 'CloverProject',
            'android_version': '14',
            'maintainer_handle': '@clover_dev'
        }

    # First call: triggers LLM and learns the project
    res1, reason1 = analyse(msg1, 'alioth', [], {'alioth': []}, llm_callable=mock_llm)
    assert reason1 == 'accepted'
    assert llm_calls == 1
    assert 'cloverproject' in search_policy.DYNAMIC_COMMUNITY_PROJECTS

    # Second call: new build with different date, different link, different version
    # Must NOT call LLM because project is now in dynamic cache!
    msg2_text = """#CloverProject #Official #ROM
CloverProject - OFFICIAL | Android 14 v2.5
Updated: 02/02/24
By @clover_dev
Download: https://mega.nz/file/222"""
    msg2 = parse_tg_channel_page(page(text=msg2_text), 'testchannel')['messages'][0]
    res2, reason2 = analyse(msg2, 'alioth', [], {'alioth': []}, llm_callable=mock_llm)
    assert reason2 == 'accepted'
    # LLM calls MUST STILL BE 1 (Cache Hit!)
    assert llm_calls == 1
    assert res2['package']['project_name'] == 'CloverProject'





