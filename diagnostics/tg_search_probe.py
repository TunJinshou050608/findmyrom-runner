"""One-shot, read-only search capability experiment. No D1, bot messages or ROMs."""
import hashlib,json,re,time,os
from pathlib import Path
from urllib.parse import urlsplit,urljoin
import requests
from bs4 import BeautifulSoup

TARGETS=['annibale','ishtar','liuqin','alioth']
report={'experiment':'tg-search-capabilities-v2','targets':TARGETS,'channels':[],'services':[]}
session=requests.Session();session.headers['User-Agent']='FindMyROM-SearchCapabilityProbe/1.0'

def fetch(url,params=None):
    started=time.monotonic()
    try:
        with session.get(url,params=params,timeout=(10,15),allow_redirects=False,stream=True) as response:
            body=bytearray()
            for chunk in response.iter_content(65536):
                body.extend(chunk)
                if len(body)>2*1024*1024:raise ValueError('MetadataLimit')
            return {'status':response.status_code,'seconds':round(time.monotonic()-started,2),'bytes':len(body),'sha256':hashlib.sha256(body).hexdigest()},body.decode('utf8','replace')
    except Exception as exc:return {'error':type(exc).__name__,'seconds':round(time.monotonic()-started,2)},''

def posts(html):
    soup=BeautifulSoup(html,'html.parser');rows={}
    for node in soup.select('.tgme_widget_message[data-post]'):
        ident=node.get('data-post','')
        if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{3,31}/[0-9]+',ident):continue
        rows[ident]=node.get_text(' ',strip=True)
    return rows

root=Path('state');latest=json.loads((root/'reports/latest.json').read_text())
# Select from the scanner's own productive cursors. No human-supplied channels.
channels=sorted(latest.get('cursors',[]),key=lambda c:-c.get('total_packages_found',0))
selected=[c['channel_username'] for c in channels if c.get('total_packages_found',0)>0][:3]
for index,channel in enumerate(selected,1):
    url='https://t.me/s/'+channel
    meta,html=fetch(url);base=posts(html);entry={'source':'productive_'+str(index),'baseline':dict(meta,messages=len(base)),'queries':[]}
    first_match=None
    for term in TARGETS+['fmr_absent_7d96b3e20c']:
        time.sleep(1.5)
        meta,html=fetch(url,{'q':term});rows=posts(html)
        matching=[k for k,t in rows.items() if term.lower() in t.lower()]
        record=dict(meta,term=term,messages=len(rows),text_matches=len(matching),same_ids_as_baseline=bool(base) and set(rows)==set(base),
            search_form=bool(BeautifulSoup(html,'html.parser').select('form input[name="q"]')))
        entry['queries'].append(record)
        if matching and first_match is None:first_match=matching[0]
    if first_match:
        time.sleep(1.5)
        meta,html=fetch('https://t.me/'+first_match,{'embed':'1','mode':'tme'})
        entry['direct_embed']=dict(meta,requested_message_found=first_match in posts(html),messages=len(posts(html)))
    report['channels'].append(entry)
    print('CHANNEL_RESULT '+json.dumps(entry,sort_keys=True),flush=True)

for service,url,param in [('tgstat','https://api.tgstat.ru/posts/search','q'),('telemetr','https://api.tlmtr.io/v1/search/messages','term'),('lyzem','https://lyzem.com/search','q')]:
    for term in (TARGETS if service=='lyzem' else TARGETS[:1]):
        meta,html=fetch(url,{param:term})
        item=dict(meta,service=service,term=term,post_links=len(set(re.findall(r'https?://t\.me/[A-Za-z][A-Za-z0-9_]{3,31}/[0-9]+',html))),
            access_wall=bool(re.search(r'captcha|cloudflare|authentication required|unauthorized|invalid.*token|token.*required',html,re.I)))
        try:
            value=json.loads(html)
            if isinstance(value,dict):
                item['json_keys']=list(value)[:10]
                for key in ('status','error','message','detail'):
                    if isinstance(value.get(key),(str,int,bool)):item[key]=str(value[key])[:160]
        except ValueError:pass
        report['services'].append(item);print('SERVICE_RESULT '+json.dumps(item,sort_keys=True),flush=True)
meta,html=fetch('https://lyzem.com/')
soup=BeautifulSoup(html,'html.parser')
report['lyzem_form']={'http':meta,'forms':[{'action':f.get('action'),'method':f.get('method','get'),'fields':[n.get('name') for n in f.select('input[name]')]} for f in soup.select('form')]}
meta,html=fetch('https://search.buzz.im/')
report['services'].append(dict(meta,service='buzz',term=None))
Path('probe-summary.json').write_text(json.dumps(report,indent=2))
print('PROBE_COMPLETE '+json.dumps(report,sort_keys=True),flush=True)
