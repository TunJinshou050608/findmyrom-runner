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
    # Search-only spelling variants follow verified catalogue models. They do
    # not change the display name or infer a model from an unknown codename.
    expanded={}
    for code,names in saved.items():
        variants=list(names)
        for name in names:
            variants.append(re.sub(r'\s+','',name))
            if name.startswith('Xiaomi Pad '):
                suffix=name[len('Xiaomi Pad '):]
                variants.extend(['Mi Pad '+suffix,'MiPad'+suffix.replace(' ',''),'小米平板'+suffix.replace(' ','')])
            elif name.startswith('Xiaomi '):
                suffix=name[len('Xiaomi '):]
                variants.extend(['Mi '+suffix,'Mi'+suffix.replace(' ',''),'小米'+suffix.replace(' ','')])
            elif name.startswith('Redmi '):variants.append('红米'+name[len('Redmi '):].replace(' ',''))
        expanded[code]=list(dict.fromkeys(variants))
    return expanded


def query_aliases(names):
    selected=names[:2]
    localized=next((name for name in names if re.search(r'[\u4e00-\u9fff]',name)),None)
    return list(dict.fromkeys([*selected,*([localized] if localized else [])]))


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
    stems=[code] if re.fullmatch('[a-z][a-z0-9_]{3,31}',code) else []
    # Site IDs are keys, not Telegram usernames. Actual codenames/model aliases
    # may still supply legitimate candidates for namespaced multi-vendor IDs.
    if '-' in code:
        short=code.split('-',1)[1]
        if re.fullmatch('[a-z][a-z0-9_]{3,31}',short):stems.append(short)
    for label in names[:4]:
        name=re.sub('[^a-z0-9]','',label.lower())
        if re.fullmatch('[a-z][a-z0-9]{3,21}',name):stems.append(name)
    stems=list(dict.fromkeys(stems))
    # Interleave catalogue-derived model spellings across suffixes: testing
    # plural-only names misses otherwise identical singular Update channels.
    found=list(stems)
    for suffix in ('updates','update','_updates','_update','globalupdates','roms'):
        found.extend(stem+suffix for stem in stems)
    return list(dict.fromkeys(v for v in found if re.fullmatch('[a-z][a-z0-9_]{3,31}',v)))[:12]


def profile_context(html):
    dom=DOM();dom.feed(html)
    names=('tgme_channel_info_header_title','tgme_channel_info_description','tgme_page_title','tgme_page_description')
    return '\n'.join(n.text() for n in dom.root.walk() if any(n.has(name) for name in names))[:8192]


def linked_channels(html):
    _,channels=indexed_messages(html)
    messages,_=indexed_messages(html)
    return sorted({v['channel'] for v in channels+messages})


def text_telegram_links(text):
    urls=re.findall(r'(?i)(?<![A-Za-z0-9_.])(?:https?://)?(?:t\.me|telegram\.me)/[A-Za-z0-9_/]+',text)
    urls=['https://'+re.sub(r'^https?://','',u,flags=re.I) for u in urls]
    for handle in re.findall(r'(?i)(?:telegram|\btg|电报|\bchannel|\bfollow|\bjoin)\s*(?:channel|group|频道|群)?\s*[:：-]?\s*@([A-Za-z][A-Za-z0-9_]{3,31})',text):urls.append('https://t.me/'+handle)
    for handle in re.findall(r'(?i)tg://resolve\?domain=([A-Za-z][A-Za-z0-9_]{3,31})',text):urls.append('https://t.me/'+handle)
    return indexed_messages(''.join('<a href="'+html_module.escape(url,quote=True)+'">TG</a>' for url in urls))


def project_repositories(http,code,query=None):
    """Optional project metadata fallback; never infer compatibility from a repo name."""
    token=os.environ.get('GITHUB_TOKEN')
    headers={'Authorization':'Bearer '+token} if token else None
    url='https://api.github.com/search/repositories?'+urlencode({'q':query or code+' android in:name,description','per_page':5})
    data=json.loads(http.get(url,ttl=86400,headers=headers))
    return [r for r in data.get('items',[]) if re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',r.get('full_name',''))][:5]


def project_links(http,repository):
    token=os.environ.get('GITHUB_TOKEN');headers={'Authorization':'Bearer '+token} if token else None
    data=json.loads(http.get('https://api.github.com/repos/'+repository+'/readme',ttl=86400,headers=headers))
    if data.get('encoding')!='base64':return [],[]
    text=base64.b64decode(data.get('content','')).decode('utf-8',errors='replace')[:150000]
    return text_telegram_links(text)


def project_owner_links(http,owner):
    token=os.environ.get('GITHUB_TOKEN');headers={'Authorization':'Bearer '+token} if token else None
    data=json.loads(http.get('https://api.github.com/users/'+owner,ttl=86400,headers=headers))
    return text_telegram_links((data.get('bio') or '')+'\n'+(data.get('blog') or ''))


def project_release_links(http,repository):
    token=os.environ.get('GITHUB_TOKEN');headers={'Authorization':'Bearer '+token} if token else None
    data=json.loads(http.get('https://api.github.com/repos/'+repository+'/releases?per_page=3',ttl=86400,headers=headers))
    return text_telegram_links('\n'.join(r.get('body') or '' for r in data[:3]))
