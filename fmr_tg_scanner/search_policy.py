"""Device and resource decisions must be grounded in the actual Telegram post."""
import json
import re
from urllib.parse import urlsplit,unquote
from .rule_extractor import extract_package_by_rules,PROJECTS
from .trial import MAINSTREAM

EXTRA_PROJECTS=[('BlissROM',r'\bBliss\s*ROM\b|#BlissROM'),('Project Elixir',r'Project\s*Elixir'),('Xtended',r'\bXtended\b'),('AlphaDroid',r'AlphaDroid'),('ColtOS',r'ColtOS'),('Bootleggers',r'Bootleggers'),('PixelExperience',r'Pixel\s*Experience|#PEX\b'),('PixelPlusUI',r'Pixel\s*Plus\s*UI'),('AOSPA',r'\bAOSPA\b'),('OxygenOS',r'OxygenOS'),('ColorOS',r'ColorOS'),('OriginOS',r'OriginOS')]
MAINSTREAM_ROMS=MAINSTREAM|{'pixelexperience','pixelplusui','blissrom','project elixir','xtended','alphadroid','coltos','aospa'}
_profile_catalogue=None
_profile_patterns=[]
_profile_cache={}


def token(text,value):return bool(re.search(r'(?<![a-z0-9_])'+re.escape(value)+r'(?![a-z0-9_])',text,re.I))


def path_token(text,value):return bool(re.search(r'(?<![a-z0-9])'+re.escape(value)+r'(?![a-z0-9])',text,re.I))


def profile_devices(profile,all_names,*,aliases_only=False):
    """Longest catalogue spans win: Pad 6 Pro must not also imply Pad 6."""
    global _profile_catalogue,_profile_patterns,_profile_cache
    if all_names is not _profile_catalogue:
        _profile_catalogue=all_names;_profile_cache={}
        _profile_patterns=[(code,re.compile(r'(?<![a-z0-9_])'+re.escape(value)+r'(?![a-z0-9_])',re.I),value==code) for code,names in all_names.items() for value in [code,*names] if value]
    cache_key=(profile,aliases_only)
    if cache_key in _profile_cache:return _profile_cache[cache_key]
    spans=[(m.start(),m.end(),code) for code,pattern,is_code in _profile_patterns if not aliases_only or not is_code for m in pattern.finditer(profile)]
    # A longer alias may contain a shorter model name. Disjoint mentions of
    # both models still identify a mixed-device profile.
    ordered=sorted(spans,key=lambda x:(x[0],-x[1]));accepted=[]
    for start,end,code in ordered:
        if any(a<=start and b>=end and (a<start or b>end) for a,b,_ in accepted):continue
        accepted.append((start,end,code))
    result={code for _,_,code in accepted}
    if len(_profile_cache)>=512:_profile_cache={}
    _profile_cache[cache_key]=result
    return result


def target_evidence(message,target,names,all_names,profile=''):
    text=message.get('text','');head='\n'.join(text.splitlines()[:4])
    files=(message.get('file_attachment_name') or '')+' '+' '.join(unquote(urlsplit(u).path) for u in message.get('links',[]) if urlsplit(u).hostname not in ('t.me','telegram.me'))
    identity_text=re.sub(r'(?im)(?:ported|extracted|taken|based)\s+from\b[^\n]*|^\s*(?:donor|base device|source device)\s*[:：-][^\n]*','',text)
    tokens=re.findall(r'(?i)#([a-z][a-z0-9_-]{2,24})\b|(?:codename|device)\s*[:：-]\s*([a-z0-9_-]{3,24})\b|[\[(]\s*([a-z0-9_-]{3,24})\s*[\])]',identity_text)
    explicit={part.lower() for groups in tokens for part in groups if part and (part.lower() in all_names or part.lower()==target)}
    if explicit and target not in explicit:return None
    if len(explicit)>1:
        return {'kind':'explicit_multi_device_post','value':target,'multi_device_post':True,'listed_devices':sorted(explicit)}
    if target in explicit:return {'kind':'explicit_codename','value':target}
    if path_token(files,target):return {'kind':'target_in_download_path','value':target}
    donors=re.findall(r'(?im)(?:ported|extracted|taken|based)\s+from\s*[:：-]?\s*([^\n]+)|^\s*(?:donor|base device|source device)\s*[:：-]\s*([^\n]+)',text)
    if any(token(part,target) or any(token(part,name) for name in names) for pair in donors for part in pair if part):return None
    for alias in names:
        if len(alias)>4 and token(text,alias):
            named=profile_devices(identity_text,all_names,aliases_only=True)
            if named and target not in named:return None
            return {'kind':'catalogue_model_alias','value':alias}
    if token(head,target):return {'kind':'codename_in_release_heading','value':target}
    if token(text,target):return {'kind':'codename_in_release_body','value':target}
    # A channel name alone is not evidence. A profile must independently name
    # exactly this model, without another known target/alias in its description.
    if profile and (token(profile,target) or any(token(profile,name) for name in names)):
        if profile_devices(profile,all_names)=={target}:
            named=profile_devices(identity_text,all_names,aliases_only=True)
            if named and target not in named:return None
            return {'kind':'dedicated_channel_profile','value':profile[:300]}
    return None


def primary_release(package,message):
    """Determine the main release before examining companion recovery/kernel links."""
    lines=[line.strip() for line in message.get('text','').splitlines() if line.strip()]
    heading='\n'.join(lines[:3]);name=package.get('file_attachment_name') or package.get('package_name') or ''
    release=heading+'\n'+name
    if re.search(r'(?i)\b(?:ROM|GSI)\b|(?:Bootleggers|PixelExperience|PixelPlusUI|Pixel\s*Plus\s*UI|AOSPA|OxygenOS|ColorOS|OriginOS)',release):kind='rom'
    elif re.search(r'(?i)\b(?:TWRP|OrangeFox|OFRP|recovery)\b',heading):kind='recovery'
    elif re.search(r'(?i)\bkernel\b|AnyKernel',release):kind='kernel'
    elif re.search(r'(?i)(?:^|[/_ -])(?:init_|vendor_)?boot\.img(?:\.gz)?$',name):kind='boot_image'
    else:kind=package['resource_kind']
    projects=[*PROJECTS,*EXTRA_PROJECTS]
    project=next((p for line in lines[:3] for p,pattern in projects if re.search(pattern,line,re.I)),None)
    if project is None:project=next((p for p,pattern in projects if re.search(pattern,name,re.I)),None)
    if project:package['project_name']=project
    elif kind!=package['resource_kind']:package['project_name']={'rom':'Custom ROM','kernel':'Kernel','recovery':'Recovery','boot_image':'Boot image'}.get(kind,package['project_name'])
    if project and project not in ('TWRP','OrangeFox','HyperOS','MIUI'):kind='rom'
    if project in ('HyperOS','MIUI') and re.search(r'(?i)\bport(?:ed)?\b|\bcustom\b',heading):kind='rom'
    first=lines[0] if lines else ''
    first_rom=any(re.search(pattern,first,re.I) for name,pattern in projects if name not in ('TWRP','OrangeFox'))
    if not first_rom and re.search(r'(?i)\b(?:TWRP|OrangeFox|OFRP|recovery)\b',first):
        kind='recovery';package['project_name']='TWRP' if re.search('twrp',first,re.I) else 'OrangeFox' if re.search('orangefox|ofrp',first,re.I) else 'Recovery'
    elif not first_rom and re.search(r'(?i)\bkernel\b',first):kind='kernel';package['project_name']='Kernel'
    package['resource_kind']=kind
    return heading


def analyse(message,target,names,all_names,profile=''):
    evidence=target_evidence(message,target,names,all_names,profile)
    if evidence is None:return None,'wrong_or_unproved_device'
    text=message.get('text','');head='\n'.join(text.splitlines()[:4])
    head+='\n'+(message.get('file_attachment_name') or '')
    resource_signal=bool(re.search(r'(?i)\b(?:rom|kernel|recovery|twrp|orangefox|ofrp|boot|fastboot|init_boot|gsi)\b|刷机包|内核|第三方系统',head) or any(re.search(pattern,head,re.I) for _,pattern in [*PROJECTS,*EXTRA_PROJECTS]))
    if re.search(r'(?i)security warning|scam alert|malware|phishing',head):return None,'not_a_release'
    release_signal=bool(re.search(r'(?i)\b(?:release|released|build|updated?|download|version|unofficial|official|community|port|changelog|gapps|vanilla)\b|发布|下载|更新|移植|适配',text) or message.get('has_tg_attachment'))
    if not resource_signal or not release_signal:return None,'not_a_resource'
    if re.search(r'(?i)^(?:can |could |does |is there |how |anyone |when |求|请问|有没有)',head.strip()):return None,'not_a_release'
    if re.search(r'[?？]',next(iter(text.splitlines()),'')) and not re.search(r'(?i)\b(?:released?|updated?|build|changelog)\b|发布|更新',head):return None,'not_a_release'
    package=extract_package_by_rules(message,default_codename=target,post_only=True)
    if package is None:return None,'not_a_resource'
    package['device_codename']=target
    package['delivery_type']='telegram_message';package['target_post_url']=package['tg_post_url']
    text=message.get('text','');heading=primary_release(package,message)
    # Link text may help classify an OEM announcement, but external files are
    # neither a deliverable nor an eligibility requirement and are never fetched.
    links=message.get('links',[])
    paths=' '.join(unquote(urlsplit(u).path) for u in links)
    stock_filename=bool(re.search(r'(?i)(?:miui_[A-Z0-9]+_|[a-z0-9]+_images_(?:OS\d|V\d))',paths))
    generic_image=bool(re.search(r'(?i)\bGSI\b',heading) or re.search(r'(?i)(?:ErfanGSI|NipponGSI|/Generic[ /%-]|[-_]AB[-_]\d)',paths))
    oem_link=any((urlsplit(u).hostname or '') in ('bigota.d.miui.com','hugeota.d.miui.com','dl.google.com','bkt-sgp-miui-ota-update-alisgp.oss-ap-southeast-1.aliyuncs.com') for u in links)
    project_base=re.sub(r'(?i)(?:rom|os)$','',package['project_name']).strip()
    status_text=heading+'\n'+re.sub(r'[_-]',' ',message.get('file_attachment_name') or '')
    official=bool(re.search(r'(?i)\bofficial\b',status_text) or (len(project_base)>3 and re.search(r'(?i)\b'+re.escape(project_base)+r'\s*(?:ROM|OS)?\s+Official\s+Channel\b',text)))
    if stock_filename or oem_link:label='stock_official'
    elif re.search(r'(?i)\bunofficial\b|#unofficial|\bcommunity\b|^\s*(?:status|build type)\s*[:：-]\s*unofficial',status_text):label='unofficial'
    elif re.search(r'(?i)\bport(?:ed)?\b|\bcustom\b',heading):label='custom_or_port'
    elif re.search(r'(?i)new\s+(?:OS|MIUI|HyperOS)\s+(?:fastboot|recovery)\s+update|(?:hyperos|miui)\s*(?:\d+)?\s*version\s*[:=]',text):label='stock_official'
    elif package['project_name'].lower() in ('hyperos','miui'):label='stock_official'
    elif package['resource_kind']=='rom' and package['project_name'].lower() in MAINSTREAM_ROMS and official:label='mainstream_official'
    elif package['resource_kind']=='rom' and official:label='official_project_unclassified'
    else:label='unknown'
    # A credited developer is not automatically the account that published the
    # post. Missing forward/signature evidence remains unknown in the new path.
    credited=package.get('maintainer_handle')
    explicit_credit=bool(re.search(r'(?i)(?:by|maintainer|developer|author|builder|built\s+by)\s*[:：-]?\s*(?:https?://t\.me/|@)',text))
    if not explicit_credit:package['maintainer_handle']=None
    signature=message.get('sender_author_signature') or ''
    if package['origin_type']=='author_direct' and not (explicit_credit and credited and credited.lstrip('@').lower() in signature.lower()):
        package['origin_type']='unknown'
    if package['origin_type']=='repost_aggregator' and package['original_post_url']==package['tg_post_url']:
        package['origin_type']='unknown'
    reason='accepted'
    if generic_image:reason='generic_image_donor_is_not_target'
    elif re.search(r'(?i)\bengineering\s+ROM\b',heading):reason='excluded_engineering_firmware'
    elif re.search(r'(?i)security warning|scam alert|malware|phishing',heading) and not re.search(r'(?i)\b(?:release|build|version)\b',heading):reason='not_a_release'
    elif label=='stock_official':reason='excluded_stock_firmware'
    elif label=='mainstream_official':reason='excluded_mainstream_official_rom'
    elif label=='official_project_unclassified':reason='official_rom_scope_unproved'
    elif label=='unknown' and package['resource_kind']=='rom' and package['project_name'].lower() in MAINSTREAM_ROMS:reason='mainstream_release_status_unproved'
    return {'package':package,'evidence':evidence,'classification':label,'accepted':reason=='accepted'},reason
