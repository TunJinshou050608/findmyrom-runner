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
        elif req.url.host=='lyzem.com':body='<html></html>'
        elif req.url.path=='/s/aliothupdates':
            ids=(28,12) if req.url.params.get('before')=='29' else (30,29) if phase[0] else (12,11)
            body=page(ids).replace('testchannel/','aliothupdates/')
        else:body='<html><div class="tgme_page_title">Contact</div></html>'
        return httpx.Response(200,headers={'content-type':'text/html'},text=body)
    def factory(*args,**kwargs):return SearchHTTP(*args,**kwargs,client=httpx.Client(transport=httpx.MockTransport(response)))
    first=run(tmp_path,['alioth'],max_requests=40,max_seconds=30,http_factory=factory)
    assert first['new_qualified_products']==2 and first['packages']==2
    assert first['D1_requests']==0 and first['llm_calls']==0
    assert all('sourceforge.net' not in url for url in requests)
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
    assert actual==reason and not result['accepted']


def test_multi_device_profile_does_not_prove_target():
    msg=parse_tg_channel_page(page(text='Android custom ROM release').replace('alioth-12.zip','build-12.zip'),'testchannel')['messages'][0]
    assert analyse(msg,'alioth',[],{'alioth':[],'umi':[]},'Alioth (DEAD) Umi (Discontinued)')[0] is None


def test_rom_heading_wins_over_companion_instructions():
    text='#A16 #OxygenOS #Alioth #Port<br>OxygenOS v16 Android 16<br>Install with TWRP recovery and kernel'
    msg=parse_tg_channel_page(page(text=text),'testchannel')['messages'][0]
    result,reason=analyse(msg,'alioth',[],{'alioth':[]})
    assert reason=='accepted' and result['package']['resource_kind']=='rom' and result['package']['project_name']=='OxygenOS'
