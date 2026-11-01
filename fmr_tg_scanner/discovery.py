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
from .html_parser import channel_name

SEARCH_PROVIDERS=[
 ('brave','https://search.brave.com/search','q'),
 ('bing','https://www.bing.com/search','q'),
 ('yahoo','https://search.yahoo.com/search','p'),
 ('duckduckgo','https://html.duckduckgo.com/html/','q'),
]
RESERVED={'share','joinchat','addstickers','addemoji','proxy','login','boost','invoice','contact','settings','telegram','android','gmail'}
MAX_BODY=2*1024*1024


def channel_links(text):
    text=unescape(text)
    for _ in range(2):text=unquote(text)
    channels={}
    for match in re.finditer(r'https?://(?:t\.me|telegram\.me)/(?:s/)?([A-Za-z][A-Za-z0-9_]{3,31})(?:/([1-9][0-9]*))?(?![A-Za-z0-9_])',text):
        channel=match[1].lower()
        if channel in RESERVED:continue
        try:channel_name(channel)
        except ValueError:continue
        channels.setdefault(channel,{'channel':channel,'evidence_url':f'https://t.me/{channel}'+('/'+match[2] if match[2] else '')})
    return list(channels.values())


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
                payload=loader();candidates=channel_links(payload)
                cache[key]={'retry_at':now+(86400 if candidates else 1800),'candidates':candidates,'provider':provider,'device':device}
                log({'event':'discovery_search_finished','provider':provider,'device':device,'candidate_count':len(candidates),'elapsed_ms':round((time.monotonic()-started)*1000)})
            except Exception as exc:
                candidates=[];cache[key]={'retry_at':now+1800,'candidates':[],'provider':provider,'device':device,'error_type':type(exc).__name__}
                log({'event':'discovery_provider_failed','provider':provider,'device':device,'error_type':type(exc).__name__})
        for item in candidates:found.setdefault(item['channel'],dict(item,discovered_for=device,provider=provider))
    try:
        queries=[f'"{device}" Telegram ROM unofficial',f'"{device}" site:t.me']
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
            try:repos=json.loads(bounded_get(client,'https://api.github.com/search/repositories',params={'q':device+' android','per_page':10},headers=headers)).get('items',[])
            except Exception:repos=[]
            try:issues=json.loads(bounded_get(client,'https://api.github.com/search/issues',params={'q':device+' telegram in:body','per_page':10},headers=headers)).get('items',[])
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
        if len(found)<8:lookup('github_public_metadata',device,github)
    finally:
        if owned:client.close()
    return list(found.values())[:50]
