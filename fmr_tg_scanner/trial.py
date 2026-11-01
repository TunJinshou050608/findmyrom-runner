"""Live TG01 trial: private Git state, bounded discovery, auditable metadata only."""
from collections import Counter
from datetime import datetime,timezone
import argparse
import json
import os
from pathlib import Path
import re
import signal
import time
from urllib.parse import urlsplit

from .crawler import scan_channel
from .storage import SQLiteDB
from .schema import initialize
from .cli import export_jsonl
from .html_parser import channel_name,ChannelUnavailableError
from .rule_extractor import safe_url,post_ref

MAINSTREAM={'lineageos','pixelos','crdroid','evolution x','orangefox','twrp'}


def classify(package,text=''):
    head='\n'.join(text.splitlines()[:3])
    if re.search(r'(?i)\bunofficial\b|#unofficial\b|\bcommunity\b',text):return 'unofficial'
    if re.search(r'(?i)\bport(?:ed)?\b|\bcustom\b',head):return 'custom_or_port'
    try:links=json.loads(package['download_links_json'])
    except (ValueError,KeyError):links=[]
    if any((urlsplit(u).hostname or '') in ('bigota.d.miui.com','hugeota.d.miui.com','dl.google.com','bkt-sgp-miui-ota-update-alisgp.oss-ap-southeast-1.aliyuncs.com') for u in links):return 'stock_official'
    if package['project_name'].lower() in ('hyperos','miui'):return 'stock_official'
    if package['project_name'].lower() in MAINSTREAM and re.search(r'(?i)\bofficial\b|#official\b',head):return 'mainstream_official'
    return 'unknown'


def qualification(package):
    links=json.loads(package['download_links_json'])
    if links and all(re.search(r'/(?:[a-z-]+/)?phones/[^/?]+/?$',urlsplit(url).path) for url in links):return 'index_reference'
    filename=package.get('file_attachment_name') or package.get('package_name') or ''
    if re.search(r'(?i)\.(zip|tgz|img(?:\.gz)?|tar(?:\.(gz|xz))?|bin|7z)$',filename):return 'package'
    if any(re.search(r'(?i)\.(zip|tgz|img(?:\.gz)?|bin|7z)(?:$|[/?#])',link) for link in links):return 'package'
    if package.get('build_version_tag') and package['rule_confidence']>=.8:return 'package'
    if any((urlsplit(link).hostname or '') in ('mega.nz','drive.google.com','pixeldrain.com','mediafire.com') for link in links) and package['rule_confidence']>=.8:return 'package'
    return 'unresolved_reference'


def atomic_json(path,value):
    data=json.dumps(value,ensure_ascii=False,sort_keys=True,indent=2)+'\n'
    if path.exists() and path.read_text()==data:return
    path.parent.mkdir(parents=True,exist_ok=True);tmp=path.with_suffix(path.suffix+'.tmp');tmp.write_text(data);tmp.replace(path)


def audit(db,targets,classification):
    rows=db.query('SELECT * FROM tg_discovered_packages ORDER BY channel_username,msg_id')
    errors=[];warnings=[];counts=Counter();accepted=[];references=Counter()
    for row in rows:
        kind=qualification(row)
        if kind!='package':references[kind]+=1
        else:accepted.append(row)
        key=f"{row['channel_username']}/{row['msg_id']}"
        if not row.get('package_name'):errors.append({'resource':key,'reason':'missing_package_name'})
        if targets and row['device_codename'] not in targets:errors.append({'resource':key,'reason':'wrong_target'})
        try:links=json.loads(row['download_links_json'])
        except (TypeError,ValueError):links=[]
        if not isinstance(links,list) or not links or not all(safe_url(u) for u in links):errors.append({'resource':key,'reason':'invalid_download_links'})
        if row['tg_post_url']!=f'https://t.me/{key}':errors.append({'resource':key,'reason':'bad_message_url'})
        if not post_ref(row['original_post_url']):errors.append({'resource':key,'reason':'bad_original_message_url'})
        if kind=='package':counts[classification.get(key,'unknown')]+=1
    if not accepted:warnings.append('No qualified package records yet; directory links and incomplete references do not count as products.')
    if references:warnings.append('Unresolved references are preserved under candidates/, excluded from packages/.')
    if rows and all(r['sender_id'] is None for r in rows):warnings.append('Public preview supplied no numeric sender IDs; values remain null, never inferred from maintainer names.')
    duplicates=db.query('SELECT channel_username,msg_id,COUNT(*) AS n FROM tg_discovered_packages GROUP BY channel_username,msg_id HAVING COUNT(*)>1')
    if duplicates:errors.append({'reason':'duplicate_message_rows'})
    integrity=db.query('PRAGMA integrity_check')[0]['integrity_check']
    if integrity!='ok':errors.append({'reason':'sqlite_integrity_failed'})
    cursors=db.query('SELECT channel_username,max_scanned_msg_id,min_scanned_msg_id,history_exhausted,total_packages_found FROM tg_scan_cursors ORDER BY channel_username')
    return {'ok':not errors,'packages':len(accepted),'records_stored':len(rows),'references':dict(references),'errors':errors,'warnings':warnings,'classifications':dict(counts),
        'numeric_sender_ids':sum(r['sender_id'] is not None for r in rows),'sender_usernames':sum(bool(r['sender_username']) for r in rows),
        'maintainer_handles':sum(bool(r['maintainer_handle']) for r in rows),'cursors':cursors}


def run(state_dir,*,fetcher=None,discoverer=None):
    root=Path(state_dir);config=json.loads((root/'config.json').read_text())
    targets=set(config.get('target_codenames') or config.get('site_device_codenames') or []) or None
    pages=int(config.get('pages_per_channel',5));channel_limit=int(config.get('channels_per_batch',3))
    if not 1<=pages<=20 or not 1<=channel_limit<=10:raise ValueError('Trial work budget exceeds safety limit')
    run_id=os.environ.get('GITHUB_RUN_ID',datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S'))+'-'+os.environ.get('GITHUB_RUN_ATTEMPT','1')
    log_path=root/'logs'/f'{run_id}.jsonl';log_path.parent.mkdir(parents=True,exist_ok=True)
    registry_path=root/'source_registry.json';registry=json.loads(registry_path.read_text()) if registry_path.exists() else {}
    if config.get('seed_channels'):raise ValueError('Manual channel seeds are forbidden in the autonomous trial')
    for info in registry.values():
        if info.get('last_error')=='ChannelUnavailableError' and info.get('profile_discovery_version',0)<2:
            info['next_scan_at']=0
    classification_path=root/'classifications.json';classification=json.loads(classification_path.read_text()) if classification_path.exists() else {}
    counters=Counter();now=time.time()
    def log(event):
        counters[event['event']]+=1
        if event.get('reason'):counters['reason:'+event['reason']]+=1
        if event['event'] in {'discovery_search_started','discovery_search_finished','discovery_provider_failed','discovery_github_candidates','discovery_github_repository','channel_started','channel_failed','page_fetched','page_committed','batch_audited'}:
            public={'event':event['event']}
            for field in ('candidate_count','elapsed_ms','floors','pages_fetched','packages_saved','error_type'):
                if field in event:public[field]=event[field]
            print(json.dumps(public,sort_keys=True),flush=True)
        with log_path.open('a') as output:output.write(json.dumps({'timestamp':datetime.now(timezone.utc).isoformat(),**event},ensure_ascii=False,sort_keys=True)+'\n')
    discovery_path=root/'discovery_cache.json'
    discovery_cache=json.loads(discovery_path.read_text()) if discovery_path.exists() else {}
    from .discovery import discover_channels
    for device in sorted(targets or []):
        for item in (discoverer or discover_channels)(device,discovery_cache,log):
            channel=channel_name(item['channel'])
            registry.setdefault(channel,{'seed':False,'next_scan_at':0,'last_scan_at':0,**item})
            registry[channel]['quality_score']=max(registry[channel].get('quality_score',0),item.get('quality_score',0))
    atomic_json(discovery_path,discovery_cache)
    class RecordingFetcher:
        def __init__(self):
            from .crawler import PageFetcher
            self.inner=fetcher or PageFetcher();self.messages={}
        def __call__(self,url):
            from .html_parser import parse_tg_channel_page,DOM
            from .discovery import channel_links
            html=self.inner(url);ch=urlsplit(url).path.rsplit('/',1)[-1]
            registry[ch]['profile_discovery_version']=2
            context_dom=DOM();context_dom.feed(html)
            context_parts=[n.text() for n in context_dom.root.walk() if any(n.has(name) for name in ('tgme_channel_info_description','tgme_page_description','tgme_widget_message_text','tgme_widget_message_document_title'))]
            # Telegram's own footer says "Android" on every channel. It must
            # never turn a music/account landing page into an Android source.
            phone_context=bool(re.search(r'(?i)\b(android|firmware|kernel|lineageos|hyperos|miui|xiaomi|redmi|poco|twrp|orangefox|magisk|kernelsu|flashable)\b',' '.join(context_parts)))
            registry[ch]['phone_context']=phone_context
            # An unavailable history can still be a public profile pointing to
            # its author's update channel. Follow metadata links, not files.
            candidates=channel_links(html) if phone_context and config.get('discover_linked_channels',True) else []
            if phone_context and config.get('discover_linked_channels',True):
                for name in re.findall(r'(?i)(?:channel|updates|releases|频道)\s*[:：-]?\s*@([a-z][a-z0-9_]{3,31})',html):
                    candidates.append({'channel':name.lower()})
            for item in candidates:
                candidate=item['channel']
                if candidate not in registry and len(registry)<100:
                    registry[candidate]={'seed':False,'discovered_from':url,'next_scan_at':0,'last_scan_at':0,'quality_score':100}
                    log({'event':'source_discovered','channel':candidate,'from_preview':ch})
            self.messages={(m['channel_username'],m['msg_id']):m for m in parse_tg_channel_page(html,ch)['messages']}
            return html
        def close(self):
            if not fetcher:self.inner.close()
    recording=RecordingFetcher();db=SQLiteDB(root/'state.sqlite');initialize(db);batch_failures=[];results=[]
    def event_sink(event):
        if event['event']=='package_committed':
            package=event['package'];key=package['channel_username']+'/'+str(package['msg_id']);msg=recording.messages.get((package['channel_username'],package['msg_id']),{})
            label=classify(package,msg.get('text',''));classification[key]=label;event['release_classification']=label
            event['qualification']=qualification(package)
            registry[package['channel_username']]['quality_score']=300 if qualification(package)=='package' else 150
            candidates=[]
            ref=post_ref(package['original_post_url'])
            if ref:candidates.append(ref[0])
            for link in msg.get('links',[]):
                try:
                    url=urlsplit(link)
                    if url.scheme=='https' and url.hostname=='t.me':
                        m=re.fullmatch(r'/(?:s/)?([A-Za-z][A-Za-z0-9_]{3,31})(?:/[0-9]+)?/?',url.path)
                        if m:candidates.append(m[1].lower())
                except ValueError:pass
            if config.get('discover_linked_channels',True):
                for candidate in candidates:
                    if candidate not in registry and len(registry)<100:
                        registry[candidate]={'seed':False,'discovered_from':key,'next_scan_at':0,'last_scan_at':0}
                        log({'event':'source_discovered','channel':candidate,'from_post':key})
        log(event)
    if not db.query('SELECT COUNT(*) AS n FROM tg_discovered_packages')[0]['n']:channel_limit=10
    log({'event':'batch_started','targets':sorted(targets) if targets else 'site_catalogue','max_pages_per_channel':pages,'channel_budget':channel_limit,'llm_enabled':False})
    selected=sorted((ch for ch,info in registry.items() if info.get('next_scan_at',0)<=now),key=lambda ch:(-registry[ch].get('quality_score',0),registry[ch].get('last_scan_at',0),ch))[:channel_limit]
    try:
        for channel in selected:
            try:
                log({'event':'channel_started','channel':channel})
                registry[channel]['profile_discovery_version']=2
                result=scan_channel(db,channel,max_pages=pages,direction='both',enable_llm=False,max_llm_calls=0,page_fetcher=recording,
                    target_codenames=targets,event_sink=event_sink,on_page=lambda s:log({'event':'page_committed',**s}))
                results.append(result);registry[channel].update(last_scan_at=now,next_scan_at=now+480,last_error=None)
                if registry[channel].get('phone_context') is False:registry[channel]['next_scan_at']=now+86400
                log({'event':'channel_finished',**result})
                print(json.dumps({'event':'channel_progress','pages':result['pages_fetched'],'saved':result['packages_saved'],'complete':result['history_exhausted']}),flush=True)
            except Exception as exc:
                code=type(exc).__name__;batch_failures.append({'channel':channel,'error_type':code})
                delay=86400 if isinstance(exc,ChannelUnavailableError) else 600
                registry[channel].update(last_scan_at=now,next_scan_at=now+delay,last_error=code)
                log({'event':'channel_failed','channel':channel,'error_type':code,'retry_after_seconds':delay})
        for row in db.query('SELECT DISTINCT channel_username FROM tg_discovered_packages'):
            export_jsonl(db,row['channel_username'],root/'candidates'/f"{row['channel_username']}.jsonl")
            export_jsonl(db,row['channel_username'],root/'packages'/f"{row['channel_username']}.jsonl",predicate=lambda record:qualification(record)=='package')
        report=audit(db,targets,classification)
        if not report['packages']:
            report['errors'].append({'reason':'no_valid_target_products_yet'})
            report['ok']=False
        if not registry:
            report['errors'].append({'reason':'autonomous_discovery_found_no_sources'})
            report['ok']=False
        report.update(timestamp=datetime.now(timezone.utc).isoformat(),run_id=run_id,channel_failures=batch_failures,
            batch_counts=dict(counters),batch_results=results,source_count=len(registry),targets=sorted(targets) if targets else None)
        if batch_failures:report['warnings'].append('Some channels failed; their committed pages are retained and retries are delayed.')
        if counters.get('reason:unsupported_preview'):report['warnings'].append('Telegram omitted content from some public previews; those floors are logged as unsupported, not claimed as extracted.')
        atomic_json(root/'reports'/f'{run_id}.json',report);atomic_json(root/'reports/latest.json',report)
        log({'event':'batch_audited','ok':report['ok'],'packages':report['packages'],'errors':len(report['errors']),'channel_failures':len(batch_failures)})
        return report
    finally:
        atomic_json(registry_path,registry);atomic_json(classification_path,classification);db.close();recording.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--state-dir',required=True);args=parser.parse_args()
    if os.environ.get('GITHUB_ACTIONS')!='true':parser.error('Live trial must run on GitHub Actions')
    def terminate(*_args):raise InterruptedError('Cloud job terminated')
    signal.signal(signal.SIGTERM,terminate)
    report=run(args.state_dir)
    print(json.dumps({'event':'trial_report','ok':report['ok'],'packages':report['packages'],'errors':len(report['errors']),'channel_failures':len(report['channel_failures'])}),flush=True)
    return 0 if report['ok'] and (report['batch_results'] or not report['channel_failures']) else 1

if __name__=='__main__':raise SystemExit(main())
