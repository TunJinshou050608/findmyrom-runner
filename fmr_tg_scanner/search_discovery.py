"""TG-specific message indexes and evidence-derived channel names; no web search engines."""
import json
import base64
import html as html_module
import os
import re
from urllib.parse import urlencode,urlsplit,urljoin
import yaml
from .html_parser import DOM,channel_name
from .rule_extractor import post_ref
from fmr_ingest_stock.device_identity import clean_model_name


def aliases(http,store,targets):
    saved=store.meta('device_aliases_v3',{})
    if not all(code in saved for code in targets):
        raw=http.get('https://raw.githubusercontent.com/XiaomiFirmwareUpdater/miui-updates-tracker/master/data/devices.yml',ttl=604800)
        for key,labels in (yaml.safe_load(raw) or {}).items():
            code=str(key).lower().split('_',1)[0]
            if not re.fullmatch('[a-z0-9_-]{3,24}',code) or not isinstance(labels,list) or not labels:continue
            label=clean_model_name(labels[0]);items=saved.setdefault(code,[])
            # Aliases are catalogue evidence, never guessed from a codename.
            parts=[label,*re.split(r'\s*/\s*|\s+aka\s+',label,flags=re.I)]
            for part in parts:
                if re.search(r'[A-Za-z]',part) and re.search(r'\d',part) and part not in items:items.append(part)
        for code in targets:saved.setdefault(code,[])
        store.set_meta('device_aliases_v3',saved)
    return saved


def indexed_messages(html):
    dom=DOM();dom.feed(html);messages={};channels={}
    for node in dom.root.walk():
        if node.tag!='a':continue
        raw=node.attrs.get('href','')
        try:
            p=urlsplit(raw)
            if p.scheme!='https' or p.hostname not in ('t.me','telegram.me') or p.username or p.password:continue
            path=p.path.removeprefix('/s/') if p.path.startswith('/s/') else p.path.lstrip('/')
            match=re.fullmatch(r'([A-Za-z][A-Za-z0-9_]{3,31})(?:/([1-9][0-9]*))?/?',path)
            if not match:continue
            channel=channel_name(match[1])
            if channel in {'share','joinchat','proxy','addstickers','addemoji','telegram','android','gmail'}:continue
            if match[2]:
                mid=int(match[2]);messages[(channel,mid)]={'channel':channel,'msg_id':mid,'url':f'https://t.me/{channel}/{mid}'}
            else:channels[channel]={'channel':channel}
        except ValueError:continue
    return list(messages.values()),list(channels.values())


def index_search(http,provider,query):
    if provider=='lyzem':url='https://lyzem.com/search?'+urlencode({'q':query})
    else:raise ValueError('Provider has not passed message-search capability validation')
    html=http.get(url,ttl=3600)
    if re.search(r'(?i)verify (?:you are|that you)|captcha|checking your browser|access denied',html):
        raise ValueError('Index challenge; no bypass attempted')
    messages,channels=indexed_messages(html)
    return {'messages':messages,'channels':channels,'source_url':url,'status':'results' if messages else 'no_message_results'}


def name_candidates(code,names):
    found=[code+'updates',code+'_updates',code]
    for name in names[:4]:
        name=re.sub('[^a-z0-9]','',name.lower())
        if 4<=len(name)<=22:found.extend([name+'updates',name+'globalupdates',name+'_updates'])
        if name.startswith('xiaomipad'):found.append('mipad'+name[len('xiaomipad'):]+'updates')
    found.extend([code+'roms',code+'_roms'])
    return list(dict.fromkeys(v for v in found if len(v)<=32))[:12]


def profile_context(html):
    dom=DOM();dom.feed(html)
    names=('tgme_channel_info_header_title','tgme_channel_info_description','tgme_page_title','tgme_page_description')
    return '\n'.join(n.text() for n in dom.root.walk() if any(n.has(name) for name in names))[:8192]


def linked_channels(html):
    _,channels=indexed_messages(html)
    messages,_=indexed_messages(html)
    return sorted({v['channel'] for v in channels+messages})


def project_repositories(http,code):
    """Optional project metadata fallback; never infer compatibility from a repo name."""
    token=os.environ.get('GITHUB_TOKEN')
    headers={'Authorization':'Bearer '+token} if token else None
    url='https://api.github.com/search/repositories?'+urlencode({'q':code+' android in:name,description','per_page':5})
    data=json.loads(http.get(url,ttl=86400,headers=headers))
    return [r['full_name'] for r in data.get('items',[]) if re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',r.get('full_name',''))][:5]


def project_links(http,repository):
    token=os.environ.get('GITHUB_TOKEN');headers={'Authorization':'Bearer '+token} if token else None
    data=json.loads(http.get('https://api.github.com/repos/'+repository+'/readme',ttl=86400,headers=headers))
    if data.get('encoding')!='base64':return [],[]
    text=base64.b64decode(data.get('content','')).decode('utf-8',errors='replace')[:150000]
    urls=re.findall(r'https://(?:t\.me|telegram\.me)/[A-Za-z0-9_/]+',text)
    return indexed_messages(''.join('<a href="'+html_module.escape(url,quote=True)+'">TG</a>' for url in urls))
