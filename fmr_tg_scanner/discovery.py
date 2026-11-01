"""Device-only autonomous public-source discovery; no baked-in Telegram channels."""
import base64
import hashlib
from html import unescape
import json
import os
import re
import time
from urllib.parse import unquote,urlsplit,parse_qs

import httpx
import yaml
from .html_parser import channel_name

SEARCH_PROVIDERS=[
 ('duckduckgo_lite_v2','https://lite.duckduckgo.com/lite/','q'),
 ('google','https://www.google.com/search','q'),
 ('yandex','https://yandex.com/search/','text'),
 ('brave','https://search.brave.com/search','q'),
 ('bing','https://www.bing.com/search','q'),
 ('yahoo','https://search.yahoo.com/search','p'),
 ('duckduckgo','https://html.duckduckgo.com/html/','q'),
]
RESERVED={'share','joinchat','addstickers','addemoji','proxy','login','boost','invoice','contact','settings','telegram','android','gmail'}
MAX_BODY=2*1024*1024


def channel_links(text):
    text=unescape(text).replace('\\/','/')
    for _ in range(2):text=unquote(text)
    channels={}
    for match in re.finditer(r'(?:https?://)?(?:t\.me|telegram\.me)/(?:s/)?([A-Za-z][A-Za-z0-9_]{3,31})(?:/([1-9][0-9]*))?(?![A-Za-z0-9_])',text):
        channel=match[1].lower()
        if channel in RESERVED:continue
        try:channel_name(channel)
        except ValueError:continue
        channels.setdefault(channel,{'channel':channel,'evidence_url':f'https://t.me/{channel}'+('/'+match[2] if match[2] else '')})
    return list(channels.values())


def organic_candidates(payload,device):
    from .html_parser import DOM
    dom=DOM();dom.feed(payload);results=[];current=None
    for node in dom.root.walk():
        if node.tag=='a' and node.has('result-link'):
            current={'url':node.attrs.get('href',''),'title':node.text()};results.append(current)
        elif node.has('result-snippet') and current is not None:current['snippet']=node.text()
    if not results:return []
    found={}
    for item in results:
        evidence=item['title']+' '+item.get('snippet','')
        if not re.search(r'(?i)(?<![a-z0-9_])'+re.escape(device)+r'(?![a-z0-9_])',evidence):continue
        if not re.search(r'(?i)android|firmware|kernel|lineage|hyperos|xiaomi|redmi|poco|recovery|#rom|unofficial',evidence):continue
        candidates=channel_links(item['url']+' '+evidence)
        for name in re.findall(r'@([a-zA-Z][a-zA-Z0-9_]{3,31})',evidence):
            if name.lower() not in RESERVED:candidates.append({'channel':name.lower(),'evidence_url':item['url']})
        for candidate in candidates:found.setdefault(candidate['channel'],dict(candidate,quality_score=200))
    return list(found.values())


def bounded_get(client,url,**kwargs):
    with client.stream('GET',url,**kwargs) as r:
        if r.status_code!=200:raise RuntimeError('Discovery HTTP '+str(r.status_code))
        body=bytearray()
        for chunk in r.iter_bytes():
            body.extend(chunk)
            if len(body)>MAX_BODY:raise ValueError('Discovery metadata size limit')
        return body.decode('utf-8',errors='replace')


def discover_channels(device,cache,log,*,client=None,now=None):
    if not re.fullmatch(r'[a-z0-9_-]{3,24}',device):raise ValueError('Invalid device codename')
    now=time.time() if now is None else now;owned=client is None
    client=client or httpx.Client(timeout=15,follow_redirects=False,headers={'User-Agent':'Mozilla/5.0'})
    found={}
    def lookup(provider,query,loader):
        key=hashlib.sha256((provider+'|'+query).encode()).hexdigest()
        old=cache.get(key)
        if old and old['retry_at']>now:
            log({'event':'discovery_cache_hit','provider':provider,'device':device,'candidate_count':len(old['candidates'])})
            candidates=old['candidates']
        else:
            started=time.monotonic()
            log({'event':'discovery_search_started','provider':provider,'device':device})
            try:
                payload=loader();candidates=organic_candidates(payload,device) if provider.startswith('duckduckgo_lite') else channel_links(payload)
                if provider.startswith('telegram_naming_aliases'):
                    for item in candidates:item['quality_score']=250
                cache[key]={'retry_at':now+(86400 if candidates else 1800),'candidates':candidates,'provider':provider,'device':device}
                log({'event':'discovery_search_finished','provider':provider,'device':device,'candidate_count':len(candidates),'elapsed_ms':round((time.monotonic()-started)*1000)})
            except Exception as exc:
                candidates=[];cache[key]={'retry_at':now+1800,'candidates':[],'provider':provider,'device':device,'error_type':type(exc).__name__}
                log({'event':'discovery_provider_failed','provider':provider,'device':device,'error_type':type(exc).__name__})
        for item in candidates:found.setdefault(item['channel'],dict(item,discovered_for=device,provider=provider))
    try:
        alias_key='device_aliases_v1:'+device
        alias_entry=cache.get(alias_key)
        if alias_entry and alias_entry['retry_at']>now:
            aliases=alias_entry.get('aliases',[])
        else:
            aliases=[]
            try:
                catalogue=yaml.safe_load(bounded_get(client,'https://raw.githubusercontent.com/XiaomiFirmwareUpdater/miui-updates-tracker/master/data/devices.yml'))
                if isinstance(catalogue,dict):
                    for code,labels in catalogue.items():
                        if str(code).split('_',1)[0].lower()==device and isinstance(labels,list) and labels:
                            for index,label in enumerate(labels):
                                if index and not re.search(r'\d',str(label)):continue
                                for value in re.split(r'\s*/\s*|\s*&\s*|\s+aka\s+',str(label),flags=re.I):
                                    alias=re.sub(r'\([^)]*\)','',value).strip()[:100]
                                    if alias and alias not in aliases:aliases.append(alias)
                cache[alias_key]={'retry_at':now+604800,'aliases':aliases}
                log({'event':'device_aliases_resolved','device':device,'aliases':aliases})
            except Exception as exc:
                cache[alias_key]={'retry_at':now+1800,'aliases':[]}
                log({'event':'device_alias_resolution_failed','device':device,'error_type':type(exc).__name__})
        def telegram_candidates():
            from .html_parser import parse_tg_channel_page
            from .crawler import pause
            verified=[]
            # These are generic naming rules, derived from the input only.
            # No device-specific channel, author, or model alias is embedded.
            model_names=set()
            for alias in aliases:
                normalized=re.sub('[^a-z0-9]','',alias.lower())
                if not 4<=len(normalized)<=28:continue
                model_names.add(normalized)
                family=re.sub(r'(?:pro|ultra|plus|max|5g|4g)$','',normalized)
                if len(family)>=4:model_names.add(family)
            names=[]
            for model in sorted(model_names,key=lambda value:(len(value),value)):
                names.extend([model,model+'update',model+'updates',model+'_updates'])
            names.extend([device,device+'update',device+'updates',device+'_updates',device+'roms',device+'_roms',device+'_official'])
            names=list(dict.fromkeys(names))[:24]
            for name in names:
                if len(name)>32:continue
                pause()
                try:
                    page=parse_tg_channel_page(bounded_get(client,'https://t.me/s/'+name),name)
                    context=' '.join(message.get('text','') for message in page['messages'])
                    if page['messages'] and re.search(r'(?i)android|firmware|kernel|lineage|hyperos|xiaomi|redmi|poco|recovery|unofficial',context):
                        verified.append('https://t.me/'+name)
                        log({'event':'discovery_telegram_candidate_verified','channel':name,'device':device,'floors':len(page['messages'])})
                except Exception as exc:
                    log({'event':'discovery_telegram_candidate_rejected','channel':name,'device':device,'error_type':type(exc).__name__})
            return '\n'.join(verified)
        lookup('telegram_naming_aliases_v2',device,telegram_candidates)
        queries=[device+' Telegram ROM',f'"{device}" Android unofficial site:t.me']
        queries.extend(alias+' unofficial Android Telegram' for alias in aliases[:2])
        for provider,url,param in SEARCH_PROVIDERS:
            for query in queries:
                params={param:query}
                if provider=='bing':params['format']='rss'
                lookup(provider,query,lambda url=url,params=params:bounded_get(client,url,params=params))
            if len(found)>=8:break
        # Independent fallback: public Android development metadata often links
        # its maintainers' update channels. No manually chosen channel is supplied.
        headers={'Accept':'application/vnd.github+json'}
        if os.environ.get('GITHUB_TOKEN'):headers['Authorization']='Bearer '+os.environ['GITHUB_TOKEN']
        def github():
            texts=[]
            repos=[];seen_repos=set()
            for query in [device+' t.me in:readme',device+' telegram in:readme',device+' android']:
                try:
                    batch=json.loads(bounded_get(client,'https://api.github.com/search/repositories',params={'q':query,'per_page':10},headers=headers)).get('items',[])
                    for repo in batch:
                        if repo['full_name'] not in seen_repos:repos.append(repo);seen_repos.add(repo['full_name'])
                except Exception:pass
            repos=repos[:20]
            try:issues=json.loads(bounded_get(client,'https://api.github.com/search/issues',params={'q':device+' t.me','per_page':10},headers=headers)).get('items',[])
            except Exception:issues=[]
            log({'event':'discovery_github_candidates','repository_count':len(repos),'issue_count':len(issues),'device':device})
            texts.extend((r.get('body') or '') for r in issues)
            owners=set()
            for repo in repos:
                log({'event':'discovery_github_repository','repository':repo['full_name'],'device':device})
                texts.append(repo.get('description') or '')
                owner=repo['owner']['login'];owners.add(owner)
                try:
                    readme=json.loads(bounded_get(client,'https://api.github.com/repos/'+repo['full_name']+'/readme',headers=headers))
                    if readme.get('encoding')=='base64':texts.append(base64.b64decode(readme['content']).decode('utf-8',errors='replace'))
                except Exception:pass
            for owner in sorted(owners)[:5]:
                try:
                    user=json.loads(bounded_get(client,'https://api.github.com/users/'+owner,headers=headers))
                    texts.extend([user.get('bio') or '',user.get('blog') or ''])
                    profile=json.loads(bounded_get(client,f'https://api.github.com/repos/{owner}/{owner}/readme',headers=headers))
                    if profile.get('encoding')=='base64':texts.append(base64.b64decode(profile['content']).decode('utf-8',errors='replace'))
                except Exception:pass
            return '\n'.join(texts)
        if len(found)<8:lookup('github_public_metadata_v2',device,github)
        def github_code():
            texts=[]
            for query in [device+' "t.me"',device+' telegram']:
                try:
                    result=json.loads(bounded_get(client,'https://api.github.com/search/code',params={'q':query,'per_page':10},headers=headers))
                    items=result.get('items',[])
                    log({'event':'discovery_code_index','device':device,'file_count':len(items)})
                    for item in items:
                        url=item.get('url','')
                        if not url.startswith('https://api.github.com/repos/'):continue
                        try:
                            content=json.loads(bounded_get(client,url,headers=headers))
                            if content.get('encoding')=='base64':texts.append(base64.b64decode(content['content']).decode('utf-8',errors='replace'))
                        except Exception:pass
                except Exception as exc:
                    log({'event':'discovery_code_index_failed','device':device,'error_type':type(exc).__name__})
            return '\n'.join(texts)
        if len(found)<8:lookup('github_code_index_v1',device,github_code)
        def searx():
            from .rule_extractor import safe_url
            data=json.loads(bounded_get(client,'https://searx.space/data/instances.json'))
            instances=[]
            for url,info in data.get('instances',{}).items():
                if not url.startswith('https://') or not safe_url(url) or info.get('network_type') not in (None,'normal'):continue
                if info.get('http',{}).get('status_code')!=200:continue
                if info.get('error') or info.get('private'):continue
                instances.append(url)
            texts=[]
            for url in sorted(instances)[:4]:
                try:
                    try:
                        payload=bounded_get(client,url.rstrip('/')+'/search',params={'q':device+' Telegram ROM','format':'json'})
                        result=json.loads(payload)
                        for row in result.get('results',[]):texts.append(str(row.get('url',''))+' '+str(row.get('content','')))
                        count=len(result.get('results',[]))
                    except Exception:
                        # Many public instances expose ordinary HTML search but
                        # disable JSON format. Use the normal public search page.
                        payload=bounded_get(client,url.rstrip('/')+'/search',params={'q':device+' Telegram ROM'})
                        texts.append(payload);count=len(channel_links(payload))
                    log({'event':'discovery_metasearch_finished','device':device,'result_count':count})
                    if channel_links('\n'.join(texts)):break
                except Exception as exc:log({'event':'discovery_metasearch_failed','device':device,'error_type':type(exc).__name__})
            return '\n'.join(texts)
        if len(found)<8:lookup('searx_public_registry_v2_html',device,searx)
    finally:
        if owned:client.close()
    return list(found.values())[:50]
