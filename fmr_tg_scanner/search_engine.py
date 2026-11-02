"""Autonomous query-first TG discovery with bounded work and independent cursors."""
from collections import Counter
from datetime import datetime,timezone
import json
import hashlib
import os
from pathlib import Path
import re
import time
import threading
import zlib
from urllib.parse import urlsplit

from .channel_search import search_page
from .html_parser import parse_tg_channel_page,ChannelUnavailableError
from .message_reader import read_message
from .search_discovery import aliases,index_search,name_candidates,profile_context,linked_channels,project_repositories,project_links,text_telegram_links,project_owner_links,project_release_links,query_aliases
from .search_http import SearchHTTP,WorkBudgetReached
from .search_policy import analyse,target_evidence
from .search_store import SearchStore
from .trial import atomic_json
from .rule_extractor import post_ref


def run(root,targets,*,max_requests=180,max_seconds=480,http_factory=SearchHTTP,mode='adaptive'):
    root=Path(root);root.mkdir(parents=True,exist_ok=True)
    if not 1<=len(targets)<=500 or not all(re.fullmatch('[a-z0-9_-]{3,24}',v) for v in targets):raise ValueError('Invalid target codenames')
    if not 1<=max_requests<=2000 or not 10<=max_seconds<=1800:raise ValueError('Invalid batch budget')
    if mode not in ('adaptive','query_first','latest_first'):raise ValueError('Unknown retrieval mode')
    run_id=os.environ.get('GITHUB_RUN_ID','local')+'-'+os.environ.get('FMR_TG_CYCLE',str(time.time_ns()))
    logs=root/'logs';logs.mkdir(exist_ok=True);log_path=logs/(run_id+'.jsonl')
    started=time.monotonic();events=Counter();new_products=0;first_product=None;per_device=Counter();failures=[];device_funnel={code:Counter() for code in targets}
    def emit(value):
        value=dict(value);events[value['event']]+=1
        with log_path.open('a') as handle:handle.write(json.dumps({'time':time.time(),**value},ensure_ascii=False,sort_keys=True)+'\n')
        if value['event'] in ('task_finished','task_failed','resource_decision','source_admitted','query_page'):
            public={key:value[key] for key in ('event','kind','reason','accepted','new','count','device','messages','matched_products','status') if key in value}
            print(json.dumps(public),flush=True)
    store=SearchStore(root/'search.sqlite');http=http_factory(store,emit,max_requests=max_requests,max_seconds=max_seconds)
    store.migrate_message_delivery()
    stopped=threading.Event()
    def heartbeat():
        while not stopped.wait(30):print(json.dumps({'event':'search_heartbeat','requests':http.requests,'new_qualified_products':new_products,'elapsed_seconds':round(time.monotonic()-started)}),flush=True)
    monitor=threading.Thread(target=heartbeat,daemon=True);monitor.start()
    all_names={code:[] for code in targets};outcome='completed';processed=0
    try:
        try:all_names=aliases(http,store,targets)
        except WorkBudgetReached:outcome='budget_reached'
        except Exception as exc:emit({'event':'alias_catalogue_unavailable','error_type':type(exc).__name__})
        if store.meta('scope_resolution_version')!='longest-model-v1':
            for channel,info in store.meta('sources',{}).items():
                context=store.meta('profile:'+channel,'')
                for code in info.get('targets',[]):
                    if code in targets and info.get('strategy_by_device',{}).get(code)!='history' and target_evidence({'text':''},code,all_names.get(code,[]),all_names,context):
                        store.enqueue('admit',code,{'channel':channel,'depth':info.get('depth',0)},1)
            store.set_meta('scope_resolution_version','longest-model-v1')
        def share_source(channel,context,depth):
            # An Android aggregator discovered while scanning one model is a
            # useful source for the other tracked models too. Compatibility
            # still comes from each actual post, never from this promotion.
            info=store.meta('sources',{}).get(channel,{})
            if 'history' in info.get('strategy_by_device',{}).values():return
            dedicated=[code for code in targets if target_evidence({'text':''},code,all_names.get(code,[]),all_names,context)]
            if dedicated:return
            known=info.get('targets',[])
            for other in targets:
                if other not in known:store.enqueue('query',other,{'channel':channel,'query':other,'depth':depth,'state':{},'reason':'shared_android_source'},6)
        for channel,info in store.meta('sources',{}).items():
            for code,strategy in info.get('strategy_by_device',{}).items():
                if code in targets and strategy=='history':store.enqueue('history',code,{'channel':channel,'depth':info.get('depth',0),'state':{}},2)
            share_source(channel,store.meta('profile:'+channel,''),info.get('depth',0))
        if store.meta('retrieval_mode')!=mode:
            for channel,info in store.meta('sources',{}).items():
                for code in info.get('targets',[]):
                    if code in targets:
                        key=store.enqueue('admit',code,{'channel':channel,'depth':info.get('depth',0)},15)
                        store.execute("UPDATE tg_search_tasks SET status='ready',next_at=0 WHERE key=?",(key,))
            store.set_meta('retrieval_mode',mode)
        for code in targets:
            store.enqueue('index',code,{'provider':'lyzem','query':code},40)
            store.enqueue('index',code,{'provider':'lyzem','query':code+' ROM'},46)
            if all_names.get(code):store.enqueue('index',code,{'provider':'lyzem','query':all_names[code][0]},47)
            store.enqueue('project',code,{'query':code},49)
            if not store.query('SELECT 1 FROM tg_search_records WHERE device=? AND accepted=1 LIMIT 1',(code,)):
                for word in ('rom','recovery','kernel','xiaomi'):
                    store.enqueue('project',code,{'query':code,'search_query':code+' '+word,'discovery_revision':2},4)
                for alias in query_aliases(all_names.get(code,[])):
                    if re.search(r'[\u4e00-\u9fff]',alias):store.enqueue('index',code,{'provider':'lyzem','query':alias},4)
            # Index discovery and direct channel candidates interleave with
            # retrieval; they cannot consume the whole batch before any scan.
            for index,channel in enumerate(name_candidates(code,all_names.get(code,[]))):
                unresolved=not store.query('SELECT 1 FROM tg_search_records WHERE device=? AND accepted=1 LIMIT 1',(code,))
                store.enqueue('admit',code,{'channel':channel,'depth':0},3 if unresolved and index<8 else 50+index)
        def admit(code,channel,html,depth=0):
            context=profile_context(html)
            try:page=parse_tg_channel_page(html,channel)
            except ChannelUnavailableError:
                if depth<2 and re.search(r'(?i)android|rom|firmware|kernel|xiaomi|redmi|poco|recovery',context):
                    for linked in linked_channels(html)[:8]:
                        if linked!=channel:store.enqueue('admit',code,{'channel':linked,'depth':depth+1},40)
                    emit({'event':'public_profile_links_observed','channel':channel,'device':code})
                return False
            actual='\n'.join(m['text'] for m in page['messages'])
            if not re.search(r'(?i)\b(android|rom|firmware|kernel|lineageos|hyperos|miui|xiaomi|redmi|poco|twrp|orangefox|magisk|kernelsu)\b',context+'\n'+actual):
                emit({'event':'source_rejected','device':code,'channel':channel,'context':context[:1200]})
                return False
            store.set_meta('profile:'+channel,context)
            sources=store.meta('sources',{})
            entry=sources.setdefault(channel,{'targets':[],'depth':depth})
            if code not in entry['targets']:entry['targets'].append(code)
            dedicated=target_evidence({'text':''},code,all_names.get(code,[]),all_names,context)
            strategy='history' if mode=='latest_first' or (mode=='adaptive' and dedicated and dedicated['kind']=='dedicated_channel_profile') else 'query'
            entry.setdefault('strategy_by_device',{})[code]=strategy
            store.set_meta('sources',sources)
            share_source(channel,context,depth)
            if strategy=='history':
                store.execute("UPDATE tg_search_tasks SET status='dormant' WHERE device=? AND kind='query' AND json_extract(payload,'$.channel')=?",(code,channel))
                store.enqueue('history',code,{'channel':channel,'depth':depth,'state':{}},2)
                if mode=='adaptive':store.enqueue('head',code,{'channel':channel,'depth':depth},8)
            else:
                key=store.enqueue('query',code,{'channel':channel,'query':code,'depth':depth,'state':{}},10)
                store.execute("UPDATE tg_search_tasks SET status='ready' WHERE key=?",(key,))
                for alias in query_aliases(all_names.get(code,[])):store.enqueue('query',code,{'channel':channel,'query':alias,'depth':depth,'state':{}},12)
                store.enqueue('head',code,{'channel':channel,'depth':depth},8)
            emit({'event':'source_admitted','channel':channel,'device':code,'strategy':strategy})
            return True
        def message(code,msg,source,depth=0,routed=False):
            nonlocal new_products,first_product
            if not routed:
                for other in targets:
                    if other!=code and target_evidence(msg,other,all_names.get(other,[]),all_names):
                        message(other,msg,source,depth,routed=True)
                        store.enqueue('admit',other,{'channel':msg['channel_username'],'depth':depth,'reason':'observed_device_post'},15)
            events['messages_considered']+=1
            device_funnel[code]['messages_considered']+=1
            result,reason=analyse(msg,code,all_names.get(code,[]),all_names,store.meta('profile:'+msg['channel_username'],''))
            if result is None:
                events['rejected:'+reason]+=1;device_funnel[code]['rejected:'+reason]+=1
                if reason=='not_a_resource' and depth<3 and target_evidence(msg,code,all_names.get(code,[]),all_names):
                    references={post_ref(url) for url in msg.get('links',[])}-{None}
                    for channel,mid in sorted(references)[:3]:
                        if (channel,mid)!=(msg['channel_username'],msg['msg_id']):
                            store.enqueue('post',code,{'channel':channel,'msg_id':mid,'depth':depth+1},5)
                            emit({'event':'reference_enqueued','device':code,'from_post':msg['tg_post_url'],'to_post':f'https://t.me/{channel}/{mid}'})
                if device_funnel[code]['rejected:'+reason]<=3:emit({'event':'rejection_sample','device':code,'reason':reason,'channel':msg['channel_username'],'msg_id':msg['msg_id'],'text':msg['text'][:1600],'links':msg.get('links',[])[:5]})
                return False
            package=result['package'];evidence={**result['evidence'],'source_url':source,'message_identity_verified':True,
                'source_post_url':package['original_post_url'],'numeric_sender_id_observed':package.get('sender_id') is not None,
                'author_identity_verified':False}
            already_product=bool(store.query('SELECT 1 FROM tg_search_records WHERE fingerprint=? AND accepted=1 LIMIT 1',(store.fingerprint(package),)))
            changed=store.save(package,evidence,result['classification'],reason,result['accepted'])
            device_funnel[code]['accepted_observations' if result['accepted'] else 'rejected:'+reason]+=1
            if result['accepted']:
                if depth<3:
                    for reference in sorted({post_ref(url) for url in msg.get('links',[])}-{None})[:3]:
                        channel,mid=reference
                        if reference!=(msg['channel_username'],msg['msg_id']):store.enqueue('post',code,{'channel':channel,'msg_id':mid,'depth':depth+1},5)
                per_device[code]+=1
                if changed and not already_product:
                    new_products+=1
                    device_funnel[code]['new_products']+=1
                    if first_product is None:first_product=round(time.monotonic()-started,3)
                if depth<2:
                    channels=set()
                    for url in [package['original_post_url'],*msg.get('links',[])]:
                        p=urlsplit(url)
                        if p.scheme=='https' and p.hostname=='t.me':
                            match=re.fullmatch(r'/(?:s/)?([a-z][a-z0-9_]{3,31})(?:/[0-9]+)?/?',p.path,re.I)
                            if match:channels.add(match[1].lower())
                    for channel in sorted(channels)[:8]:
                        if channel!=msg['channel_username']:store.enqueue('admit',code,{'channel':channel,'depth':depth+1},35)
            emit({'event':'resource_decision','device':code,'channel':msg['channel_username'],'msg_id':msg['msg_id'],
                  'accepted':result['accepted'],'new':changed,'reason':reason,'classification':result['classification'],'package':package,'evidence':evidence,'observed_text':msg['text'][:16000]})
            return result['accepted']
        replay=store.meta('message_contract_cache_replay',{})
        if not replay.get('complete'):
            cached=store.query("SELECT url,body FROM tg_http_cache WHERE url LIKE 'https://t.me/%' AND url>? ORDER BY url LIMIT 20",(replay.get('after',''),))
            for row in cached:
                p=urlsplit(row['url']);match=re.fullmatch(r'/s/([a-z][a-z0-9_]{3,31})',p.path)
                exact=re.fullmatch(r'/([a-z][a-z0-9_]{3,31})/([1-9][0-9]*)',p.path)
                if match or exact:
                    channel=(match or exact)[1]
                    try:
                        html=zlib.decompress(row['body']).decode()
                        if exact:html='<section class="tgme_widget_history">'+html+'</section>'
                        page=parse_tg_channel_page(html,channel)
                        for msg in page['messages']:
                            if exact and msg['msg_id']!=int(exact[2]):continue
                            for code in targets:
                                if target_evidence(msg,code,all_names.get(code,[]),all_names,store.meta('profile:'+channel,'')):message(code,msg,row['url'],routed=True)
                        emit({'event':'cached_page_rechecked','channel':channel,'messages':len(page['messages']),'external_requests':0})
                    except ChannelUnavailableError:pass
                replay['after']=row['url']
            replay['complete']=len(cached)<20;store.set_meta('message_contract_cache_replay',replay)
        last_heartbeat=time.monotonic();task=None
        while time.monotonic()-started<max_seconds:
            if time.monotonic()-last_heartbeat>=30:
                print(json.dumps({'event':'search_heartbeat','requests':http.requests,'new_qualified_products':new_products,'tasks_completed':processed}),flush=True);last_heartbeat=time.monotonic()
            task=store.due(targets)
            if task is None:break
            code,kind,payload=task['device'],task['kind'],task['payload'];depth=payload.get('depth',0)
            try:
                count=0;delay=3600;products_at_start=new_products
                if kind=='index':
                    # A random negative control is shared, not repeated per model.
                    if store.meta('lyzem_control_checked',{}).get('checked',0)+86400<time.time():
                        control=index_search(http,'lyzem','fmr_negative_zqx724601193')
                        store.set_meta('lyzem_control_checked',{'ok':not control['messages'],'checked':time.time()})
                    control=store.meta('lyzem_control_checked')
                    if not control['ok']:raise ValueError('Index ignores query; provider disabled')
                    results=index_search(http,payload['provider'],payload['query']);count=len(results['messages'])
                    for item in results['messages'][:12]:
                        priority=5 if re.search(r'(?i)android|rom|gsi|miui|kernel|recovery|updates',item['channel']) else 58
                        store.enqueue('post',code,{**item,'depth':0},priority)
                    # Bare channels are evidence candidates, never products.
                    for item in results['channels'][:6]:store.enqueue('admit',code,{**item,'depth':0},65)
                    emit({'event':'index_funnel','device':code,'candidate_messages':count,'status':results['status']})
                elif kind=='post':
                    msg,url=read_message(lambda url:http.get(url,ttl=86400),payload['channel'],payload['msg_id'])
                    count=int(message(code,msg,url,depth))
                    if count or re.search(r'(?i)android|\brom\b|kernel|recovery|twrp',msg['text']):store.enqueue('admit',code,{'channel':payload['channel'],'depth':depth},15)
                    delay=7*86400
                elif kind=='project':
                    for repository in project_repositories(http,code,payload.get('search_query')):
                        name=repository['full_name']
                        posts,channels=text_telegram_links((repository.get('description') or '')+'\n'+(repository.get('homepage') or ''))
                        for item in posts:store.enqueue('post',code,{**item,'depth':0},5)
                        for item in channels:store.enqueue('admit',code,{**item,'depth':0},15)
                        store.enqueue('readme',code,{'repository':name,'discovery_revision':2},5)
                        store.enqueue('owner',code,{'owner':name.split('/')[0]},5)
                        store.enqueue('release_links',code,{'repository':name},5)
                    delay=86400
                elif kind in ('readme','owner','release_links'):
                    if kind=='owner':posts,channels=project_owner_links(http,payload['owner'])
                    elif kind=='release_links':posts,channels=project_release_links(http,payload['repository'])
                    else:posts,channels=project_links(http,payload['repository'])
                    for item in posts[:10]:store.enqueue('post',code,{**item,'depth':0},5)
                    for item in channels[:8]:store.enqueue('admit',code,{**item,'depth':0},35)
                    count=len(posts)+len(channels);delay=86400
                    emit({'event':'project_metadata_links','device':code,'repository':payload.get('repository'),'owner':payload.get('owner'),'candidate_links':count})
                elif kind=='admit':
                    url='https://t.me/s/'+payload['channel'];html=http.get(url,ttl=300)
                    admitted=admit(code,payload['channel'],html,depth)
                    count=int(admitted);delay=86400 if admitted else 2*86400
                    # The fetched head is reused immediately rather than thrown away.
                    if admitted:
                        page=parse_tg_channel_page(html,payload['channel'])
                        for msg in page['messages']:message(code,msg,url,depth)
                        if depth<2:
                            for channel in linked_channels(html)[:12]:
                                if channel!=payload['channel']:store.enqueue('admit',code,{'channel':channel,'depth':depth+1},60)
                elif kind=='query':
                    page=search_page(lambda url:http.get(url,ttl=86400 if payload.get('state',{}).get('before') else 120),payload['channel'],payload['query'],payload.get('state'))
                    for msg in page['messages']:count+=int(message(code,msg,page['source_url'],depth))
                    payload['state']=page['state'];delay=0 if page['has_more'] else 1800
                    if not page['messages'] and payload['query']==code:
                        for alias in query_aliases(all_names.get(code,[])):store.enqueue('query',code,{'channel':payload['channel'],'query':alias,'depth':depth,'state':{}},12)
                    if page['status'] not in ('more','search_exhausted'):
                        delay=7200
                        store.enqueue('history',code,{'channel':payload['channel'],'depth':depth,'state':{}},30)
                        emit({'event':'query_incomplete','status':page['status'],'device':code,'channel':payload['channel']})
                    emit({'event':'query_page','device':code,'channel':payload['channel'],'status':page['status'],'matched_products':count,'messages':len(page['messages'])})
                elif kind in ('head','history'):
                    before=payload.get('state',{}).get('before')
                    url='https://t.me/s/'+payload['channel']+(f'?before={before}' if before else '')
                    page=parse_tg_channel_page(http.get(url,ttl=86400 if before else 120),payload['channel'])
                    for msg in page['messages']:count+=int(message(code,msg,url,depth))
                    delay=600
                    if kind=='history':
                        lo=page['min_msg_id']
                        if lo and (not before or lo<before):payload['state']={'before':lo};delay=0
                        else:delay=86400
                    else:
                        state=payload.get('state',{});anchor=state.get('anchor',0)
                        highest=max(state.get('highest',0),page['max_msg_id'] or 0)
                        lo=page['min_msg_id']
                        # Preserve the old head watermark until every page of
                        # an incremental gap has reached it, even across runs.
                        if anchor and lo and lo>anchor and (not before or lo<before):
                            payload['state']={'before':lo,'anchor':anchor,'highest':highest};delay=0
                        else:payload['state']={'anchor':highest,'highest':highest}
                else:raise ValueError('Unknown search task')
                if kind in ('head','query') and delay in (600,1800):
                    idle_key='idle:'+task['key']
                    idle=0 if new_products>products_at_start else store.meta(idle_key,0)+1
                    store.set_meta(idle_key,idle)
                    delay=min(3600 if kind=='head' else 21600,delay*(2**min(idle,4)))
                store.finish(task,delay=delay,payload=payload);processed+=1
                emit({'event':'task_finished','kind':kind,'device':code,'count':count,'next_in_seconds':delay})
            except WorkBudgetReached:
                store.finish(task,payload=payload);outcome='budget_reached';break
            except Exception as exc:
                reason=type(exc).__name__;failures.append({'kind':kind,'device':code,'error_type':reason})
                if kind=='query':store.enqueue('history',code,{'channel':payload['channel'],'depth':depth,'state':{}},30)
                retry=86400 if reason=='MessageIdentityError' else min(86400,900*(2**min(task['attempts'],5)))
                store.finish(task,delay=retry,error=reason,payload=payload)
                emit({'event':'task_failed','kind':kind,'device':code,'error_type':reason})
            task=None
        else:outcome='time_budget_reached'
        rows=store.query('SELECT * FROM tg_search_records ORDER BY device,channel,msg_id')
        unique={}
        for row in rows:
            if row['accepted']:unique.setdefault(row['fingerprint'],row)
        products=[{'package':json.loads(row['payload']),'evidence':json.loads(row['evidence']),'classification':row['classification']} for row in unique.values()]
        posts=[{'device_codename':v['package']['device_codename'],'project_name':v['package']['project_name'],'resource_kind':v['package']['resource_kind'],'title':v['package']['package_name'],'target_post_url':v['package']['tg_post_url'],'original_post_url':v['package']['original_post_url'],'classification':v['classification']} for v in products]
        atomic_json(root/'products.json',{'delivery_type':'telegram_message','posts':posts,'products':products})
        atomic_json(root/'candidates.json',{'candidates':[{'package':json.loads(r['payload']),'evidence':json.loads(r['evidence']),'reason':r['reason']} for r in rows if not r['accepted']]})
        todo=store.query("SELECT kind,status,COUNT(*) AS count FROM tg_search_tasks GROUP BY kind,status")
        counts=Counter(v['package']['device_codename'] for v in products)
        report={'engine':'tg_search_v3','mode':mode,'ok':not failures or len(failures)<processed,'has_products':bool(products),'activity':'new_products' if new_products else 'no_new_products','outcome':outcome,'targets':targets,
            'policy_version':hashlib.sha256(Path(__file__).with_name('search_policy.py').read_bytes()).hexdigest()[:16],
            'new_qualified_products':new_products,'packages':len(products),'stored_observations':len(rows),'per_device':{code:counts.get(code,0) for code in targets},
            'delivery_type':'telegram_message','telegram_posts':len(posts),'new_telegram_posts':new_products,
            'unresolved_targets':[code for code in targets if not counts.get(code)],'coverage_ok':all(counts.get(code,0) for code in targets),
            'device_funnel':{code:dict(values) for code,values in device_funnel.items()},
            'http_requests':http.requests,'cache_hits':http.cache_hits,'response_bytes':http.bytes,'http_errors':http.errors,
            'first_product_seconds':first_product,'elapsed_seconds':round(time.monotonic()-started,3),'tasks_processed':processed,
            'requests_per_new_product':round(http.requests/new_products,3) if new_products else None,
            'events':dict(events),'task_states':todo,'failures':failures,'sources':len(store.meta('sources',{})),
            'D1_requests':0,'llm_calls':0,'firmware_downloaded':False,'global_coverage_complete':False,
            'frontier_limit_observed':store.meta('frontier_limit_observed',False),
            'note':'Products are observed metadata, not binary integrity or independent author verification. Search completion is not complete Telegram history.'}
        atomic_json(root/'reports'/f'{run_id}.json',report);atomic_json(root/'reports/latest.json',report)
        print(json.dumps({k:report[k] for k in ('engine','mode','ok','outcome','new_qualified_products','packages','per_device','http_requests','cache_hits','first_product_seconds','elapsed_seconds','sources')}),flush=True)
        return report
    finally:stopped.set();monitor.join(2);http.close();store.close()
