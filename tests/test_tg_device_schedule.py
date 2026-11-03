import httpx
import pytest

from fmr_tg_scanner.device_schedule import select, complete, EMPTY_DELAYS
from fmr_tg_scanner.website_order import read_order, KEY, TTL
from fmr_tg_scanner.search_store import SearchStore


def html(links):
    return '<main>' + ''.join('<a href="'+link+'">Device</a>' for link in links) + '</main>'


def test_uses_exact_brand_page_order_and_interleaves_brands_with_cached_reads(tmp_path):
    pages = {'/': html(['/brands/xiaomi', '/brands/google']),
             '/brands/xiaomi': html(['/devices/znew', '/devices/aold', '/devices/znew']),
             '/brands/google': html(['/devices/pnew', '/devices/pold'])}
    calls = []
    def serve(request):
        calls.append(request.url.path)
        return httpx.Response(200, text=pages[request.url.path], headers={'content-type':'text/html'})
    store = SearchStore(tmp_path / 's.sqlite')
    try:
        with httpx.Client(transport=httpx.MockTransport(serve)) as client:
            names = {code: [] for code in ['aold','znew','pnew','pold','not_on_site']}
            ordered, info = read_order(store, names, client=client, now=100)
            assert ordered == ['znew','pnew','aold','pold']
            assert info['source'] == 'website_brand_pages'
            read_order(store, names, client=client, now=101)
            assert calls == ['/', '/brands/xiaomi', '/brands/google']
    finally:
        store.close()


def test_website_failure_reuses_last_order_and_never_invents_age_order(tmp_path):
    store = SearchStore(tmp_path / 's.sqlite')
    try:
        with httpx.Client(transport=httpx.MockTransport(lambda _:httpx.Response(503))) as client:
            with pytest.raises(RuntimeError, match='no inferred'):
                read_order(store, {'aaa':[]}, client=client, now=10)
            store.set_meta(KEY, {'fetched_at':0,'devices':['aaa'],'brand_counts':{'x':1}})
            ordered, info = read_order(store, {'aaa':[]}, client=client, now=TTL+1)
            assert ordered == ['aaa'] and info['stale_cache']
    finally:
        store.close()


def test_empty_devices_yield_to_entire_directory_and_backoff_is_bounded():
    ordered = [f'dev{i:04}' for i in range(1241)]
    state = {}; visited = []
    for _ in range(156):
        batch, reset = select(ordered, state, 0)
        visited.extend(batch)
        complete(state, batch, {'ok':True}, 0, reset)
    assert visited == ordered
    assert select(ordered, state, 1)[0] == []
    for empty in range(1,8):
        now = state['devices'][ordered[0]]['next_at']
        complete(state, [ordered[0]], {'ok':True}, now)
        assert state['devices'][ordered[0]]['next_at'] - now == EMPTY_DELAYS[min(empty,4)]
    complete(state, [ordered[0]], {'ok':True,'device_funnel':{ordered[0]:{'new_products':1}}}, now)
    assert state['devices'][ordered[0]]['empty_runs'] == 0
    assert state['devices'][ordered[0]]['next_at'] == now+600


def test_new_device_enters_before_unvisited_old_devices_without_restarting_round():
    state = complete({}, ['new'], {'ok':True}, 0)
    assert select(['newer','new','old'],state,1,1)[0] == ['newer']
    complete(state,['newer'],{'ok':True},1)
    assert select(['newer','new','old'],state,2,1)[0] == ['old']


def test_unattempted_budget_limited_devices_are_not_mistaken_for_empty():
    report = {'ok':True,'outcome':'budget_reached','tasks_per_device':{'new':1,'old':0}}
    state = complete({},['new','old'],report,0)
    assert state['seen'] == ['new']
    assert state['devices']['old'].get('next_at',0) == 0
    assert select(['new','old'],state,1,1)[0] == ['old']


def test_transport_failure_does_not_increment_empty_result_streak():
    state = complete({},['new'],{'ok':False},100)
    assert state['devices']['new']['next_at'] == 100+900
    assert 'empty_runs' not in state['devices']['new']


def test_engine_enforces_each_device_task_cap_without_starving_its_neighbor(tmp_path,monkeypatch):
    from fmr_tg_scanner import search_engine
    from fmr_tg_scanner.search_http import SearchHTTP
    monkeypatch.setenv('FMR_TEST_NO_SLEEP','1')
    monkeypatch.setattr(search_engine,'aliases',lambda http,store,targets:{code:[] for code in targets})
    def factory(*args,**kwargs):
        client=httpx.Client(transport=httpx.MockTransport(lambda _:httpx.Response(200,
            headers={'content-type':'text/html'},text='<html><div class="tgme_page_title">Contact</div></html>')))
        return SearchHTTP(*args,**kwargs,client=client)
    report=search_engine.run(tmp_path,['aaa','bbb'],max_requests=20,max_seconds=10,
                             http_factory=factory,enable_llm=False,max_tasks_per_device=1)
    assert report['tasks_per_device']=={'aaa':1,'bbb':1}


def test_incomplete_task_does_not_count_as_a_negative_device_probe():
    report={'ok':True,'outcome':'budget_reached','tasks_per_device':{'new':1},'incomplete_device':'new'}
    state=complete({},['new'],report,100)
    assert state['seen']==[]
    assert 'empty_runs' not in state['devices']['new']


def test_producer_exits_without_searching_when_all_devices_are_cooling(tmp_path,monkeypatch):
    from fmr_tg_scanner import search_watch
    from fmr_tg_scanner.device_schedule import KEY as SCHEDULE_KEY
    monkeypatch.setattr(search_watch,'read_catalogue',lambda:({'aaa':[]},[]))
    monkeypatch.setattr(search_watch,'read_order',lambda *args:(['aaa'],{'source':'website_brand_pages'}))
    monkeypatch.setattr(search_watch.time,'time',lambda:100)
    monkeypatch.setattr(search_watch,'run',lambda *args,**kwargs:pytest.fail('Cooling devices must not be searched'))
    (tmp_path/'search-v3').mkdir()
    store=SearchStore(tmp_path/'search-v3/search.sqlite')
    store.set_meta(SCHEDULE_KEY,{'devices':{'aaa':{'next_at':1000}},'seen':['aaa']})
    store.close()
    report=search_watch.produce(tmp_path)
    assert report['activity']=='all_devices_cooling'
    assert report['next_device_at']==1000
