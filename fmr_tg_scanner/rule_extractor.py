"""Deterministic metadata extraction; attribution is observed evidence, not identity verification."""
import hashlib
import ipaddress
import json
import re
from urllib.parse import urlsplit, unquote

MAINTAINER_REGEX=r'(?i)(?:by|maintainer|dev|developer|author|builder|built\s+by|credits?)\s*[:：\-]?\s*(?:https?://t\.me/|@)([a-zA-Z0-9_]{4,32})\b'
CODENAME_FIELD_REGEX=r'(?i)(?:device|codename|for)\s*[:：\-]?\s*[^\n\(\)]*\(([a-z0-9_-]{3,24})\)'
HASHTAG_CODENAME_REGEX=r'#([a-zA-Z][a-zA-Z0-9_]{2,20})\b'
WORDS=r'rom|boot|twrp|orangefox|ofrp|kernel|firmware|lineage|crdroid|pixelos|evolution|hyperos|miui|aosp|flash|recovery|magisk|kernelsu|vbmeta|build|update|android'
GENERIC=set('android telegram gmail rom recovery kernel firmware boot init_boot stock unofficial official stable beta update updates release releases aosp lineageos crdroid pixelos evolutionx orangefox twrp magisk kernelsu gapps vanilla hyperos miui'.split())
PROJECTS=[('AxionOS',r'axion(?:os|aosp)?'),('Evolution X',r'evolution\s*x|evox'),('OrangeFox',r'orangefox|ofrp'),('crDroid',r'crdroid'),('LineageOS',r'lineage(?:os)?'),('PixelOS',r'pixelos'),('TWRP',r'twrp'),('HyperOS',r'hyperos'),('MIUI',r'miui'),('Lunaris',r'lunaris'),('Project Infinity X',r'project\s*infinity\s*x|projectinfinityx'),('MistOS',r'mistos'),('RisingOS',r'risingos')]

def download_evidence(url,label=''):
    host=(urlsplit(url).hostname or '').lower();path=urlsplit(url).path
    if host in ('linktr.ee','paypal.me','paypal.com','www.paypal.com','ko-fi.com','buymeacoffee.com','patreon.com'):return False
    positive=bool(re.search(r'(?i)\b(download|gapps|vanilla|rom|boot|recovery|kernel)\b|下载',label))
    if not positive and re.search(r'(?i)\b(installation|instructions|screenshot|features|support|donate|changelog|credits|source)\b',label):return False
    if re.search(r'(?i)\.(zip|tgz|tar(?:\.(?:gz|xz|zst))?|img(?:\.gz)?|bin|7z)(?:$|[/?#])',url):return True
    if host in ('mega.nz','drive.google.com','pixeldrain.com','mediafire.com','androidfilehost.com','www.androidfilehost.com'):return True
    if host=='sourceforge.net' and '/files/' in path:return True
    if host=='github.com' and '/releases' in path:return True
    return positive


def safe_url(value):
    try:
        if not isinstance(value,str) or len(value)>4096 or re.search(r'[\x00-\x20\x7f\\]',value):return None
        u=urlsplit(value);host=(u.hostname or '').lower().rstrip('.')
        if u.scheme not in ('http','https') or not host or '.' not in host or u.username or u.password or u.port not in (None,80,443):return None
        if host.endswith(('.localhost','.local','.internal','.lan','.test','.invalid')) or host in ('localhost','metadata.google.internal'):return None
        try:
            if not ipaddress.ip_address(host).is_global:return None
        except ValueError:
            if re.fullmatch(r'[0-9.]+',host) or host.startswith('0x'):return None
        return value
    except (ValueError,UnicodeError):return None


def post_ref(url):
    try:
        u=urlsplit(url)
        if u.scheme!='https' or u.hostname not in ('t.me','telegram.me') or u.username or u.password:return None
        m=re.fullmatch(r'/([a-zA-Z][a-zA-Z0-9_]{3,31})/([1-9][0-9]*)/?',u.path)
        if m:return m[1].lower(),int(m[2])
    except ValueError:pass
    return None


def handle(value):
    v=value.lstrip('@').lower()
    return '@'+v if re.fullmatch(r'[a-z0-9_]{4,32}',v) and v not in GENERIC else None


def extract_package_by_rules(msg_dict: dict, default_codename=None, *, post_only=False):
    m=msg_dict;text=m.get('text','');channel=m['channel_username'];url=m['tg_post_url']
    labels={}
    for item in m.get('link_items',[]):labels[item['url']]=labels.get(item['url'],'')+' '+item.get('label','')
    links=[]
    for link in ([] if post_only else m.get('links',[])+re.findall(r'https?://[^\s<>"\']+',text)):
        link=link.rstrip('.,);')
        if not safe_url(link):continue
        u=urlsplit(link)
        if u.hostname in ('t.me','telegram.me'):
            ref=post_ref(link)
            if not ref or ref[0]==channel:continue
            # Other-channel post links identify provenance, not a downloadable file.
            continue
        if not download_evidence(link,labels.get(link,'')):continue
        if link not in links:links.append(link)
    attachment=m.get('has_tg_attachment',False) and bool(re.search(r'(?i)\.(zip|tgz|tar(?:\.(gz|xz|zst))?|img(?:\.gz)?|bin|7z)$',m.get('file_attachment_name') or ''))
    if attachment and not post_only:links.append(url)
    if (not links and not post_only) or (len(text)<25 and not re.search(r'\.(?:zip|img|tgz)(?:\.gz)?$',m.get('file_attachment_name') or '',re.I)) or not re.search(WORDS,text+' '+(m.get('file_attachment_name') or ''),re.I):return None
    match=re.search(MAINTAINER_REGEX,text)
    maintainer=handle(match[1]) if match else None
    if not maintainer:
        for link in m.get('links',[]):
            try:u=urlsplit(link)
            except ValueError:continue
            if u.hostname in ('t.me','telegram.me') and re.fullmatch(r'/[a-zA-Z0-9_]{4,32}',u.path):
                candidate=handle(u.path[1:])
                if candidate and candidate!='@'+channel:maintainer=candidate;break
    original=post_ref(m.get('forwarded_from_url') or '')
    source=re.search(r'(?i)\b(?:source|via|from)\s*[:：-]?\s*(https://t\.me/[A-Za-z0-9_]+/[0-9]+)',text)
    source_ref=post_ref(source[1]) if source else None
    if not source_ref and re.search(r'(?i)\b(?:source|via|from)\s*[:：-]',text):
        source_ref=next((post_ref(link) for link in m.get('links',[]) if post_ref(link) and post_ref(link)[0]!=channel and link!=m.get('forwarded_from_url')),None)
    signature=m.get('sender_author_signature')
    signed=bool(maintainer and signature and maintainer.lstrip('@') in signature.lower())
    if m.get('has_forward_header'):
        origin='native_forward'
    elif source_ref and source_ref[0]!=channel:
        origin='repost_aggregator';original=source_ref
    elif maintainer and maintainer!='@'+channel and re.search('updates|hub|channel|news|cloud|releases',channel) and not signed:
        origin='repost_aggregator'
    else:origin='author_direct'
    if not maintainer and origin=='author_direct':maintainer=handle(channel)
    code=None
    field=re.search(CODENAME_FIELD_REGEX,text) or re.search(r'(?i)(?:device|codename|for)\s*[:：-]?\s*[^\n\[\]]*\[([a-z0-9_-]{3,24})\]',text)
    explicit=re.search(r'(?i)\bcodename\s*[:：-]\s*([a-z0-9_-]{3,24})',text)
    if field:code=field[1].lower()
    elif explicit:code=explicit[1].lower()
    else:
        for tag in re.findall(HASHTAG_CODENAME_REGEX,text):
            if tag.lower() not in GENERIC:code=tag.lower();break
    code=code or default_codename
    if code and not re.fullmatch(r'[a-z0-9_-]{3,24}',code):code=None
    combined=text+' '+(m.get('file_attachment_name') or '')
    lower=combined.lower()
    kind='boot_image' if re.search(r'(?:init_|vendor_)?boot|vbmeta|\.img(?:\.gz)?\b',lower) and not re.search(r'twrp|orangefox|recovery',lower) else 'recovery' if re.search(r'twrp|orangefox|ofrp|recovery',lower) else 'kernel' if 'kernel' in lower else 'firmware' if re.search('firmware|hyperos|miui',lower) else 'rom'
    heading=next((line.strip() for line in text.splitlines() if line.strip()),'Resource')
    project=next((name for name,pattern in PROJECTS if re.search(pattern,heading,re.I)),None)
    if project is None and not re.search(r'(?i)\brom\b',heading):project=next((name for name,pattern in PROJECTS if re.search(pattern,combined,re.I)),None)
    if project in ('TWRP','OrangeFox'):kind='recovery'
    elif project in ('HyperOS','MIUI'):kind='firmware'
    elif project or re.search(r'(?i)\brom\b',heading):kind='rom'
    if kind=='rom':
        links=[link for link in links if not re.search(r'(?i)\b(recovery|boot|vbmeta)\b',labels.get(link,'')) and not re.search(r'(?i)(orangefox|twrp|init_boot|vendor_boot|vbmeta)',urlsplit(link).path)]
        if not links and not post_only:return None
    recognized=bool(project or re.search(r'\b(?:rom|recovery|kernel|firmware|boot|init_boot|vbmeta)\b',combined,re.I))
    if not project and recognized:project={'boot_image':'Boot image','kernel':'Kernel','firmware':'Firmware','recovery':'Recovery','rom':'Custom ROM'}[kind]
    title=heading[:40]
    package_name=m.get('file_attachment_name')
    if not package_name:
        for link in links:
            segments=urlsplit(link).path.rstrip('/').split('/')
            filename=unquote(segments[-2] if len(segments)>1 and segments[-1].lower() in ('download','view') else segments[-1])
            if re.search(r'(?i)\.(?:zip|tgz|tar(?:\.(?:gz|xz|zst))?|img(?:\.gz)?|bin|7z)$',filename):
                package_name=filename;break
    package_name=re.sub(r'[\x00-\x1f\x7f]','',package_name or heading)[:512]
    sender_id=m.get('sender_id')
    if type(sender_id) is not int or not -(2**53)<sender_id<2**53 or sender_id==0:
        sender_id=None
    sender_username=handle(m.get('sender_username') or '')
    android=re.search(r'(?i)\bandroid\s*[:：-]?\s*(\d{1,2}(?:\.\d+)?)\b',text)
    version_pattern=r'(?i)\b(?:OS|V)\d+(?:\.[A-Za-z0-9]+){1,6}\b|\b(?:v\d+(?:\.[\w-]+)+|R\d+(?:\.[\w-]+)*|\d{8}|\d{4}-\d{2}-\d{2})\b'
    version=re.search(version_pattern,heading)
    version_tag=version[0] if version else None
    if not version_tag and project:
        pattern=next((pattern for name,pattern in PROJECTS if name==project),None)
        primary=re.search('(?:'+pattern+r')\s*[-:|]?\s*(?P<version>v?\d+(?:\.\d+){1,4})',text,re.I) if pattern else None
        if primary:version_tag=primary['version']
    if not version_tag:
        version=re.search(version_pattern,text);version_tag=version[0] if version else None
    confidence=round(.30+(.25 if recognized else 0)+(.25 if code else 0)+(.20 if match or android else 0),2)
    return {'channel_username':channel,'msg_id':m['msg_id'],'tg_post_url':url,'origin_type':origin,
        'is_forwarded':int(origin=='native_forward'),'original_channel_username':original[0] if original else None,
        'original_msg_id':original[1] if original else None,'original_post_url':f'https://t.me/{original[0]}/{original[1]}' if original else url,
        'sender_author_signature':signature,'maintainer_handle':maintainer,'maintainer_display_name':None,
        'package_name':package_name,'sender_id':sender_id,'sender_username':sender_username,
        'device_codename':code,'resource_kind':kind,'project_name':project or title,
        'android_version':android[1] if android else None,'build_version_tag':version_tag,
        'download_links_json':json.dumps(links,ensure_ascii=False),'has_tg_attachment':int(attachment),
        'file_attachment_name':m.get('file_attachment_name'),'extraction_method':'rule_only','rule_confidence':confidence,
        'published_timestamp':m.get('published_timestamp'),'raw_text_hash':hashlib.sha256(text.encode()).hexdigest()}
