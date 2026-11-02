"""Atomic pages, two historical cursors and durable in-flight incremental gaps."""
import json
import os
import random
import time
import re
from urllib.parse import urlsplit,urljoin

import httpx
from .html_parser import parse_tg_channel_page, channel_name, ChannelUnavailableError
from .rule_extractor import extract_package_by_rules
from .schema import initialize,COLUMNS
from . import llm_fallback


class ScanHTTPError(RuntimeError): pass


def pause(seconds=None):
    if os.environ.get('FMR_TEST_NO_SLEEP')!='1':time.sleep(random.uniform(1,2) if seconds is None else min(max(seconds,1),60))


class PageFetcher:
    def __init__(self):self.client=httpx.Client(timeout=30,follow_redirects=False,headers={'User-Agent':'Mozilla/5.0'})
    def __call__(self,url):
        parsed=urlsplit(url)
        if parsed.scheme!='https' or parsed.netloc!='t.me' or not re.fullmatch(r'/s/[a-z][a-z0-9_]{3,31}',parsed.path) or parsed.fragment or (parsed.query and not re.fullmatch(r'before=[1-9][0-9]*',parsed.query)):
            raise ValueError('Scanner fetches public preview pages only, never package URLs')
        for attempt in range(3):
            pause()
            try:
                with self.client.stream('GET',url) as response:
                    if response.status_code in (301,302,307,308):
                        destination=urljoin(url,response.headers.get('Location',''))
                        expected='https://t.me/'+parsed.path.rsplit('/',1)[-1]
                        if destination.rstrip('/')!=expected:
                            raise ChannelUnavailableError('Preview redirected away from the requested public profile')
                        # Read only the same account's public landing HTML. This
                        # discovers profile-to-channel links, never an attachment.
                        with self.client.stream('GET',destination) as profile:
                            if profile.status_code==429 or profile.status_code>=500:
                                if attempt==2:raise ScanHTTPError('Profile preview rate/server error')
                                pause(2**attempt);continue
                            if profile.status_code!=200:raise ChannelUnavailableError('Public profile unavailable')
                            body=bytearray()
                            for chunk in profile.iter_bytes():
                                body.extend(chunk)
                                if len(body)>4*1024*1024:raise ScanHTTPError('Profile body exceeds limit')
                            return body.decode('utf-8',errors='replace')
                    if response.status_code==429 or response.status_code>=500:
                        if attempt==2:raise ScanHTTPError('Preview rate/server error')
                        try:delay=float(response.headers.get('Retry-After',2**attempt))
                        except ValueError:delay=2**attempt
                        pause(delay);continue
                    if response.status_code!=200:raise ChannelUnavailableError('Public preview unavailable')
                    body=bytearray()
                    for chunk in response.iter_bytes():
                        body.extend(chunk)
                        if len(body)>4*1024*1024:raise ScanHTTPError('Preview body exceeds limit')
                    return body.decode('utf-8',errors='replace')
            except (httpx.TimeoutException,httpx.TransportError):
                if attempt==2:raise ScanHTTPError('Preview network failure') from None
        raise ScanHTTPError('Preview unavailable')
    def close(self):self.client.close()


def upsert(package):
    package=dict(package)
    package.setdefault('taxonomy_json','{}')
    # Unchanged content must not generate another UPDATE/write or another row.
    updates=','.join(f'{c}=excluded.{c}' for c in COLUMNS if c not in ('channel_username','msg_id','maintainer_handle'))
    updates+=',maintainer_handle=COALESCE(excluded.maintainer_handle,tg_discovered_packages.maintainer_handle)'
    differs=' OR '.join(f'tg_discovered_packages.{c} IS NOT excluded.{c}' for c in COLUMNS if c not in ('channel_username','msg_id','maintainer_handle'))
    return (f"INSERT INTO tg_discovered_packages({','.join(COLUMNS)}) VALUES ({','.join('?' for _ in COLUMNS)}) ON CONFLICT(channel_username,msg_id) DO UPDATE SET {updates} WHERE {differs}",tuple(package[c] for c in COLUMNS))


def scan_channel(db_client, channel_username: str, default_codename=None, max_pages: int=10,
                 direction: str='both', enable_llm: bool=True, max_llm_calls: int=20, page_fetcher=None,
                 *, llm_callable=None, on_page=None, target_codenames=None, event_sink=None):
    if direction not in ('both','incremental','backfill') or not 1<=max_pages<=5000 or not 0<=max_llm_calls<=500:raise ValueError('Invalid scanner limits')
    channel=channel_name(channel_username);db=db_client;initialize(db)
    rows=db.query('SELECT * FROM tg_scan_cursors WHERE channel_username=?',(channel,))
    cursor=rows[0] if rows else {'max_scanned_msg_id':0,'min_scanned_msg_id':0,'history_exhausted':0,'total_packages_found':0,'scan_state_json':'{}'}
    state=json.loads(cursor['scan_state_json']);high=cursor['max_scanned_msg_id'];low=cursor['min_scanned_msg_id'];exhausted=bool(cursor['history_exhausted'])
    pending=state.get('incremental');mode='new' if not high or direction!='backfill' else 'old'
    if direction=='backfill' and pending:
        # A previous catch-up gap is completed before backfill; never misclassify it as covered.
        mode='new'
    before=pending['before'] if pending and mode=='new' else low if mode=='old' else None
    anchor=pending['anchor'] if pending else high;target=pending['target'] if pending else high
    stats={'channel':channel,'pages_fetched':0,'messages_inspected':0,'packages_saved':0,'skipped_noise_count':0,'llm_calls_used':0}
    targets={str(code).lower() for code in target_codenames} if target_codenames else None
    def event(name,**data):
        if event_sink:event_sink({'event':name,'channel':channel,**data})
    owns=page_fetcher is None;fetch=page_fetcher or PageFetcher()
    try:
        while stats['pages_fetched']<max_pages:
            if mode=='old' and exhausted:break
            url='https://t.me/s/'+channel+(f'?before={before}' if before else '')
            event('page_fetch_start',direction=mode,before=before)
            page=parse_tg_channel_page(fetch(url),channel);stats['pages_fetched']+=1
            event('page_fetched',floors=len(page['messages']),page_min=page['min_msg_id'],page_max=page['max_msg_id'])
            lo,hi=page['min_msg_id'],page['max_msg_id'];cold=high==0
            messages=[m for m in page['messages'] if cold or (mode=='new' and m['msg_id']>anchor) or (mode=='old' and m['msg_id']<low)]
            packages=[];saved=0
            for msg in messages:
                stats['messages_inspected']+=1
                existing=db.query('SELECT * FROM tg_discovered_packages WHERE channel_username=? AND msg_id=?',(channel,msg['msg_id']))
                package=extract_package_by_rules(msg,default_codename)
                if package is None:
                    stats['skipped_noise_count']+=1;event('message_skipped',msg_id=msg['msg_id'],reason='unsupported_preview' if msg.get('preview_unavailable') else 'no_resource_evidence');continue
                if targets:
                    strong=[code for code in targets if re.search(r'(?i)(?:#'+re.escape(code)+r'\b|[\[(]\s*'+re.escape(code)+r'\s*[\])])',msg['text'])]
                    if len(strong)==1:package['device_codename']=strong[0]
                    if not package['device_codename']:
                        detected=[code for code in targets if re.search(r'(?<![a-z0-9_])'+re.escape(code)+r'(?![a-z0-9_])',msg['text'],re.I)]
                        if len(detected)==1:package['device_codename']=detected[0]
                    if package['device_codename'] not in targets:
                        event('message_skipped',msg_id=msg['msg_id'],reason='target_mismatch',detected_codename=package['device_codename']);continue
                from .taxonomy import classify, VERSION as taxonomy_version
                if existing and json.loads(existing[0].get('taxonomy_json') or '{}').get('schema_version')==taxonomy_version and all(existing[0][k]==package[k] for k in ('raw_text_hash','download_links_json','original_post_url','origin_type','sender_author_signature','has_tg_attachment','file_attachment_name','published_timestamp','package_name','sender_id','sender_username')):
                    event('message_skipped',msg_id=msg['msg_id'],reason='unchanged');continue
                digest=package['raw_text_hash'];known=db.query('SELECT * FROM tg_discovered_packages WHERE raw_text_hash=? LIMIT 1',(digest,))
                if known and known[0]['extraction_method']=='rule_plus_llm':
                    for name in llm_fallback.FIELDS:
                        if not package.get(name) or name=='project_name' and package['rule_confidence']<.8:package[name]=known[0].get(name)
                    package['extraction_method']='rule_plus_llm'
                elif .30<=package['rule_confidence']<.80 and enable_llm and (llm_callable or os.environ.get('LLM_API_KEY')) and stats['llm_calls_used']<max_llm_calls and not known and digest not in state.get('llm_attempted',[]):
                    # Persist attempt intent before spending money. A crash during an
                    # API call falls back to rules on retry instead of billing twice.
                    state.setdefault('llm_attempted',[]).append(digest)
                    state['llm_attempted']=state['llm_attempted'][-2000:]
                    db.execute("INSERT INTO tg_scan_cursors(channel_username,scan_state_json) VALUES (?,?) ON CONFLICT(channel_username) DO UPDATE SET scan_state_json=excluded.scan_state_json",(channel,json.dumps(state,sort_keys=True)))
                    stats['llm_calls_used']+=1
                    try:package=llm_fallback.merge(package,(llm_callable or llm_fallback.completion)(msg['text']),text=msg['text'])
                    except Exception:pass
                package['taxonomy_json']=json.dumps(classify(msg,package,accepted=True),ensure_ascii=False,sort_keys=True)
                if package.get('device_codename') or package['extraction_method']=='rule_plus_llm':
                    packages.append(package);saved+=1
                else:event('message_skipped',msg_id=msg['msg_id'],reason='unknown_codename')
            if cold:
                if hi is None:exhausted=True
                else:high=hi;low=lo;anchor=hi;exhausted=lo<=1
                next_mode='old';next_before=low
            elif mode=='new':
                target=max(target,hi or 0)
                # No new progress or reaching the stable anchor completes catch-up.
                if before and lo is not None and lo>=before:
                    raise ScanHTTPError('Incremental pagination did not advance; checkpoint retained')
                complete=lo is None or lo<=anchor
                if complete:
                    high=max(high,target);state.pop('incremental',None);next_mode='old';next_before=low
                else:
                    state['incremental']={'anchor':anchor,'target':target,'before':lo};next_mode='new';next_before=lo
            else:
                if lo is None or lo>=low:exhausted=True
                else:low=lo;exhausted=lo<=1
                next_mode='old';next_before=low
            statements=[upsert(p) for p in packages]
            statements.append(("""INSERT INTO tg_scan_cursors(channel_username,max_scanned_msg_id,min_scanned_msg_id,history_exhausted,total_packages_found,updated_at,scan_state_json)
                VALUES (?,?,?,?,(SELECT COUNT(*) FROM tg_discovered_packages WHERE channel_username=?),strftime('%s','now'),?)
                ON CONFLICT(channel_username) DO UPDATE SET max_scanned_msg_id=excluded.max_scanned_msg_id,min_scanned_msg_id=excluded.min_scanned_msg_id,
                history_exhausted=excluded.history_exhausted,total_packages_found=excluded.total_packages_found,updated_at=excluded.updated_at,scan_state_json=excluded.scan_state_json
                WHERE tg_scan_cursors.max_scanned_msg_id IS NOT excluded.max_scanned_msg_id
                  OR tg_scan_cursors.min_scanned_msg_id IS NOT excluded.min_scanned_msg_id
                  OR tg_scan_cursors.history_exhausted IS NOT excluded.history_exhausted
                  OR tg_scan_cursors.total_packages_found IS NOT excluded.total_packages_found
                  OR tg_scan_cursors.scan_state_json IS NOT excluded.scan_state_json""",
                (channel,high,low,int(exhausted),channel,json.dumps(state,sort_keys=True))))
            db.execute_batch(statements);stats['packages_saved']+=saved
            for package in packages:event('package_committed',package=package)
            if on_page:on_page(dict(stats,min_scanned_msg_id=low,max_scanned_msg_id=high,history_exhausted=exhausted))
            if direction=='incremental' and next_mode=='old':break
            mode=next_mode;before=next_before
    finally:
        if owns:fetch.close()
    return dict(stats,min_scanned_msg_id=low,max_scanned_msg_id=high,history_exhausted=exhausted)
