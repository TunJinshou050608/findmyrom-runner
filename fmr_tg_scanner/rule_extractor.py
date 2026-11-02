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
GENERIC=set('android telegram gmail rom recovery kernel firmware boot init_boot stock unofficial official stable beta update updates release releases aosp lineageos crdroid pixelos evolutionx orangefox twrp magisk kernelsu gapps vanilla hyperos miui clo qpr0 qpr1 qpr2 qpr3 hotfix new'.split())
GENERIC_MAINTAINERS=set('all everyone team community coolapk someone notes official channel unknown admin the google xiaomi poco'.split())

PROJECTS=[
    ('Paranoid Android',r'paranoid\s*android|\baospa\b'),
    ('AxionOS',r'axion(?:os|aosp)?'),
    ('Evolution X',r'evolution\s*x|evox'),
    ('OrangeFox',r'orangefox|ofrp'),
    ('crDroid',r'crdroid'),
    ('LineageOS',r'lineage(?:os)?'),
    ('PixelOS',r'pixelos'),
    ('TWRP',r'twrp'),
    ('HyperOS',r'hyperos'),
    ('MIUI',r'miui'),
    ('Lunaris',r'lunaris(?:aosp|os)?'),
    ('Project Infinity X',r'project\s*infinity\s*x|projectinfinityx|\binfinity\s*x\b'),
    ('MistOS',r'mistos'),
    ('RisingOS',r'rising(?:os)?'),
    ('DerpFest',r'derpfest'),
    ('VoltageOS',r'voltage(?:os)?'),
    ('AfterLife',r'afterlife'),
    ('SyberiaOS',r'syberia(?:os|project)?'),
    ('AICP',r'\baicp\b'),
    ('AncientOS',r'ancient(?:os)?'),
    ('AviumUI',r'avium(?:ui)?'),
    ('AwakenOS',r'awaken(?:os)?'),
    ('Matrixx',r'project\s*matrixx|\bmatrixx\b'),
    ('SuperiorOS',r'superior(?:os)?'),
    ('SparkOS',r'spark(?:os)?'),
    ('CherishOS',r'cherish(?:os)?'),
    ('ArrowOS',r'arrow(?:os)?'),
    ('HavocOS',r'havoc(?:os)?'),
    ('Corvus',r'corvus'),
    ('OctaviOS',r'octavi(?:os)?'),
    ('BlissROM',r'\bBliss\s*ROM\b|#BlissROM'),
    ('Project Elixir',r'Project\s*Elixir'),
    ('Xtended',r'\bXtended\b'),
    ('AlphaDroid',r'AlphaDroid'),
    ('ColtOS',r'ColtOS'),
    ('Bootleggers',r'Bootleggers'),
    ('PixelExperience',r'Pixel\s*Experience|#PEX\b'),
    ('PixelPlusUI',r'Pixel\s*Plus\s*UI'),
    ('AOSPA',r'\bAOSPA\b'),
    ('OxygenOS',r'OxygenOS'),
    ('ColorOS',r'ColorOS'),
    ('OriginOS',r'OriginOS'),
    ('XPerience',r'XPerience'),
    ('Project Zephyrus',r'Project\s*Zephyrus'),
    ('OrionOS',r'OrionOS'),
    ('ShapeShift',r'ShapeShift(?:\s*OS)?|\bssos\b'),
    ('ConquerOS',r'ConquerOS'),
    ('AOSP Extended',r'AOSP\s*Extended|\baex\b'),
    ('Xiaomi.eu',r'xiaomi\.eu|xiaomieu'),
]

LETTER_MAP={
    'Q':'10','R':'11','S':'12','12L':'12.1','T':'13',
    'U':'14','V':'15','B':'16','BAKLAVA':'16','C':'17'
}
VERSION_PATTERN=r'(?i)\b(?:OS|V)\d+(?:\.[A-Za-z0-9]+){1,6}\b|\b(?:v\d+(?:\.[\w-]+)*|R\d+(?:\.[\w-]+)*|\d{8}|\d{4}-\d{2}-\d{2})\b'


def extract_clean_heading(text: str) -> str:
    lines=[line.strip() for line in text.splitlines() if line.strip()]
    if not lines:return 'Resource'
    idx=0
    while idx<min(3,len(lines)):
        l=lines[idx]
        is_tag_line=bool(
            re.fullmatch(r'(?:#[A-Za-z0-9_-]+\s*)+',l) or
            (len(re.findall(r'#[A-Za-z0-9_-]+',l))>=2 and len(re.sub(r'#[A-Za-z0-9_-]+','',l).strip())<8)
        )
        if is_tag_line and idx+1<len(lines):
            idx+=1
        else:
            break
    h=lines[idx]
    cleaned=re.sub(r'^[#\s📲🗿🔹🔸▪️▫️•*\-]+','',h).strip()
    return cleaned if cleaned else lines[0]


def dynamic_project_name(heading: str, text: str, first_line: str, kind: str) -> str | None:
    for name,pattern in PROJECTS:
        if re.search(pattern,heading,re.I):return name
    m=re.search(r'\b([A-Za-z0-9_-]{2,20})\s*(?:OS|UI|AOSP|ROM)\b',heading)
    if m and m[1].lower() not in GENERIC:
        suffix='OS' if 'os' in m[0].lower() else ('UI' if 'ui' in m[0].lower() else '')
        return m[1]+(suffix if not m[1].lower().endswith(suffix.lower()) else '')
    first_part=re.split(r'\s*[|•▪️▫️🔹🔸\-]\s*',heading)[0].strip()
    name_m=re.match(r'^([A-Za-z][A-Za-z0-9_-]{2,20})(?:\s+(?:v?[\d.]+|alpha|beta))?$',first_part,re.I)
    if name_m and name_m[1].lower() not in GENERIC:
        return name_m[1]
    tags=re.findall(r'#([A-Za-z0-9_-]{3,24})\b',first_line)
    for tag in tags:
        if tag.lower() not in GENERIC and tag.lower() not in ('official','unofficial','port','community','clo','qpr0','qpr1','qpr2','gapps','vanilla'):
            return tag
    return None


def extract_android_version(text: str, heading: str, filename: str = '') -> str | None:
    combined=heading+'\n'+text+'\n'+filename
    m=re.search(r'(?i)\bandroid\s*[:：-]?\s*(\d{1,2}(?:\.\d+)?)\b',combined)
    if m:return m[1]
    m=re.search(r'(?i)(?:^|[\s|#•▪️▫️🔹🔸])A(1[0-7])\b',combined)
    if m:return m[1]
    m=re.search(r'(?:#|\|\s*)([RSTUVBC]|12L|Baklava)\b',combined,re.I)
    if m:
        val=m[1].upper()
        if val in LETTER_MAP:return LETTER_MAP[val]
    return None


def extract_build_version_tag(text: str, heading: str, project: str = '') -> str | None:
    lines=[l.strip() for l in text.splitlines() if l.strip()]
    search_scope=[heading]+lines[:3]
    for l in search_scope:
        m=re.search(r'(?i)\b(?:version|ver|build)\s*[:：-]?\s*v?(\d+(?:\.[\w-]+)+)\b',l)
        if m:return m[1]
    if project and project not in ('Kernel','Recovery','Custom ROM','Firmware','Boot image'):
        for l in search_scope:
            m = re.search(r'(?i)\b' + re.escape(project) + r'(?:\s*(?:AOSP|OS|UI|ROM))?\s*[-:|]?\s*(v?\d+(?:\.[\w-]+)*)\b', l)
            if m:
                val = m[1]
                if val not in ('11', '12', '13', '14', '15', '16', '17') or re.search(r'(?i)\b' + re.escape(project) + r'\s+' + val + r'\b', l):
                    return val
    m=re.search(VERSION_PATTERN,heading)
    if m:return m[0]
    m=re.search(r'\b(\d+\.\d+(?:\.[\w-]+)*)\b',heading)
    if m:return m[1]
    m=re.search(VERSION_PATTERN,text)
    if m:return m[0]
    return None


def extract_maintainer_info(text: str, links: list, channel: str, signature: str | None = None):
    m=re.search(r'(?i)(?:by|maintainer|dev|developer|author|builder|built\s+by)\s*[:：\-]?\s*(?:https?://t\.me/|@)([a-zA-Z0-9_]{3,32})\b',text)
    if m and m[1].lower() not in GENERIC_MAINTAINERS:
        h=handle(m[1])
        if h:return h,m[1]
    m=re.search(r'(?im)^[^\n\w]*(?:maintainer|developer|builder|dev|author|built\s+by|by)\s*[:：\-]\s*([a-zA-Z0-9_][a-zA-Z0-9_\-\. ]{2,28})',text)
    if m:
        name=m[1].strip()
        name=re.split(r'\s+(?:on\s+coolapk|on\s+xda|[|(\[\n])',name,flags=re.I)[0].strip()
        if name and name.lower() not in GENERIC_MAINTAINERS and len(name)>=3:
            h=handle(name) if re.fullmatch(r'[a-zA-Z0-9_]{3,32}',name) else None
            return h,name
    for link in links:
        try:
            u=urlsplit(link)
            if u.hostname in ('t.me','telegram.me') and re.fullmatch(r'/[a-zA-Z0-9_]{4,32}',u.path):
                cand=handle(u.path[1:])
                if cand and cand!='@'+channel:return cand,u.path[1:]
        except ValueError:pass
    if signature and len(signature.strip())>=3 and not re.search(r'(?i)\b(?:updates?|channel|news|cloud|repo|screenshots?)\b',signature):
        sig=signature.strip()
        h=handle(sig) if re.fullmatch(r'[a-zA-Z0-9_]{3,32}',sig) else None
        return h,sig
    return None,None


def download_evidence(url,label=''):
    host=(urlsplit(url).hostname or '').lower();path=urlsplit(url).path
    if host in ('linktr.ee','paypal.me','paypal.com','www.paypal.com','ko-fi.com','buymeacoffee.com','patreon.com'):return False
    positive=bool(re.search(r'(?i)\b(download|gapps|vanilla|rom|boot|recovery|kernel|fastboot|mirror|gdrive|pixeldrain)\b|下载',label))
    if not positive and re.search(r'(?i)\b(installation|instructions|screenshot|features|support|donate|changelog|credits|source|about|rules)\b',label):return False
    if re.search(r'(?i)\.(zip|tgz|tar(?:\.(?:gz|xz|zst))?|img(?:\.gz)?|bin|7z)(?:$|[/?#])',url):return True
    if host in ('mega.nz','drive.google.com','pixeldrain.com','mediafire.com','androidfilehost.com','www.androidfilehost.com','sourceforge.net','zdrive.to','gofile.io','1drv.ms','onedrive.live.com','terabox.com','cloud.mail.ru','yadi.sk'):return True
    if host=='github.com' and ('/releases' in path or '/archive/' in path):return True
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


def strip_instruction_and_credit_sections(text):
    lines = []
    in_sub = False
    for line in text.splitlines():
        if re.search(r'(?i)^\s*(?:notes?|instructions?|how to flash|flashing (?:guide|instructions?)|credits?|thanks to|special thanks|huge thanks)\s*[:：-]', line):
            in_sub = True
            continue
        elif in_sub:
            if re.match(r'(?i)^\s*(?:#|build date|updated|maintainer|link|download|changelog|\w+\s*(?:v?\d|\bcommunity|\bunofficial|\bofficial))', line):
                in_sub = False
            else:
                continue
        lines.append(line)
    return '\n'.join(lines)


def handle(value):
    v=value.lstrip('@').lower()
    return '@'+v if re.fullmatch(r'[a-z0-9_]{4,32}',v) and v not in GENERIC else None


def extract_package_by_rules(msg_dict: dict, default_codename=None, *, post_only=False):
    m=msg_dict;text=m.get('text','');channel=m['channel_username'];url=m['tg_post_url']
    labels={}
    for item in m.get('link_items',[]):labels[item['url']]=labels.get(item['url'],'')+' '+item.get('label','')
    links=[]
    candidate_links = m.get('links', []) + re.findall(r'https?://[^\s<>"\']+', text)
    for link in candidate_links:
        link=link.rstrip('.,);')
        if not safe_url(link):continue
        u=urlsplit(link)
        if u.hostname in ('t.me','telegram.me'):
            ref=post_ref(link)
            if not ref or ref[0]==channel:continue
            continue
        if not download_evidence(link,labels.get(link,'')):continue
        if link not in links:links.append(link)
    attachment=m.get('has_tg_attachment',False) and bool(re.search(r'(?i)\.(zip|tgz|tar(?:\.(gz|xz|zst))?|img(?:\.gz)?|bin|7z)$',m.get('file_attachment_name') or ''))
    if attachment and not post_only:links.append(url)
    if (not post_only and (not links or (len(text)<25 and not re.search(r'\.(?:zip|img|tgz)(?:\.gz)?$',m.get('file_attachment_name') or '',re.I)))) or not re.search(WORDS,text+' '+(m.get('file_attachment_name') or ''),re.I):return None

    signature=m.get('sender_author_signature')
    maintainer, maintainer_name = extract_maintainer_info(text, m.get('links', []), channel, signature)

    original=post_ref(m.get('forwarded_from_url') or '')
    source=re.search(r'(?i)\b(?:source|via|from)\s*[:：-]?\s*(https://t\.me/[A-Za-z0-9_]+/[0-9]+)',text)
    source_ref=post_ref(source[1]) if source else None
    if not source_ref and re.search(r'(?i)\b(?:source|via|from)\s*[:：-]',text):
        source_ref=next((post_ref(link) for link in m.get('links',[]) if post_ref(link) and post_ref(link)[0]!=channel and link!=m.get('forwarded_from_url')),None)
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
    core_text = strip_instruction_and_credit_sections(text)
    combined = core_text + ' ' + (m.get('file_attachment_name') or '')
    lower = combined.lower()
    
    first_line = next((line.strip() for line in text.splitlines() if line.strip()), '')
    heading = extract_clean_heading(text)
    head_lower = heading.lower()
    first_lower = first_line.lower()

    first_rom = any(re.search(pattern, first_line, re.I) for name, pattern in PROJECTS if name not in ('TWRP', 'OrangeFox', 'Xiaomi.eu', 'MIUI', 'HyperOS'))
    first_recovery = bool(re.search(r'(?i)\b(?:TWRP|OrangeFox|OFRP|SHRP|PBRP|recovery)\b', first_lower))
    first_kernel = bool(re.search(r'(?i)\b(?:kernel|anykernel)\b', first_lower)) and not bool(re.search(r'(?i)\b(?:rom|ui|os)\b', first_lower))

    is_recovery = bool(re.search(r'(?i)\b(?:twrp|orangefox|ofrp|shrp|pbrp|skyhawk|recovery)\b', head_lower))
    is_kernel = bool(re.search(r'(?i)\b(?:kernel|anykernel)\b', head_lower)) and not bool(re.search(r'(?i)\b(?:android|aosp|rom|ui|os)\b', head_lower))
    is_boot = bool(re.search(r'(?i)(?:^|[/_ -])(?:init_|vendor_)?boot\.img(?:\.gz)?$', m.get('file_attachment_name') or ''))
    is_rom = bool(re.search(r'(?i)\b(?:android\s*\d+|a\d{2}|rom|aosp|gsi)\b', head_lower) or re.search(r'(?i)\b[a-z0-9_-]+(?:os|ui)\b', head_lower))

    if not first_rom and first_recovery: kind = 'recovery'
    elif not first_rom and first_kernel: kind = 'kernel'
    elif is_recovery: kind = 'recovery'
    elif is_kernel: kind = 'kernel'
    elif is_boot: kind = 'boot_image'
    elif is_rom: kind = 'rom'
    elif re.search(r'\b(?:init_|vendor_)?boot\b|\bvbmeta\b|\.img(?:\.gz)?\b', lower) and not re.search(r'twrp|orangefox|recovery', lower): kind = 'boot_image'
    elif re.search(r'twrp|orangefox|ofrp|recovery', lower): kind = 'recovery'
    elif 'kernel' in lower: kind = 'kernel'
    elif re.search('firmware', lower): kind = 'firmware'
    else: kind = 'rom'

    project = dynamic_project_name(heading, text, first_line, kind)
    if not project and not re.search(r'(?i)\brom\b', heading):
        project = next((name for name, pattern in PROJECTS if re.search(pattern, combined, re.I)), None)

    recognized=bool(project or re.search(r'\b(?:rom|recovery|kernel|firmware|boot|init_boot|vbmeta)\b',combined,re.I))
    if not project and recognized:
        project={'boot_image':'Boot image','kernel':'Kernel','firmware':'Firmware','recovery':'Recovery','rom':'Custom ROM'}.get(kind,'Custom ROM')

    if kind=='rom':
        links=[link for link in links if not re.search(r'(?i)\b(recovery|boot|vbmeta)\b',labels.get(link,'')) and not re.search(r'(?i)(orangefox|twrp|init_boot|vendor_boot|vbmeta)',urlsplit(link).path)]
        if not links and not post_only:return None

    title=heading[:40]
    cleaned_title = re.split(r'\s+[•▪️]\s*', heading)[0].strip()
    package_name=m.get('file_attachment_name')
    if not package_name:
        for link in links:
            segments=urlsplit(link).path.rstrip('/').split('/')
            filename=unquote(segments[-2] if len(segments)>1 and segments[-1].lower() in ('download','view') else segments[-1])
            if re.search(r'(?i)\.(?:zip|tgz|tar(?:\.(?:gz|xz|zst))?|img(?:\.gz)?|bin|7z)$',filename):
                package_name=filename;break
    package_name=re.sub(r'[\x00-\x1f\x7f]','',package_name or cleaned_title)[:160]
    sender_id=m.get('sender_id')
    if type(sender_id) is not int or not -(2**53)<sender_id<2**53 or sender_id==0:
        sender_id=None
    sender_username=handle(m.get('sender_username') or '')
    
    android=extract_android_version(text, heading, m.get('file_attachment_name') or '')
    version_tag=extract_build_version_tag(text, heading, project or '')
    
    match=bool(re.search(MAINTAINER_REGEX,text) or (maintainer and maintainer!='@'+channel))
    confidence=round(.30+(.25 if recognized else 0)+(.25 if code else 0)+(.20 if match or android else 0),2)
    return {'channel_username':channel,'msg_id':m['msg_id'],'tg_post_url':url,'origin_type':origin,
        'is_forwarded':int(origin=='native_forward'),'original_channel_username':original[0] if original else None,
        'original_msg_id':original[1] if original else None,'original_post_url':f'https://t.me/{original[0]}/{original[1]}' if original else url,
        'sender_author_signature':signature,'maintainer_handle':maintainer,'maintainer_display_name':maintainer_name,
        'package_name':package_name,'sender_id':sender_id,'sender_username':sender_username,
        'device_codename':code,'resource_kind':kind,'project_name':project or title,
        'android_version':android,'build_version_tag':version_tag,
        'download_links_json':'[]' if post_only else json.dumps(links,ensure_ascii=False),'has_tg_attachment':int(attachment),
        'file_attachment_name':m.get('file_attachment_name'),'extraction_method':'rule_only','rule_confidence':confidence,
        'published_timestamp':m.get('published_timestamp'),'raw_text_hash':hashlib.sha256(text.encode()).hexdigest()}
