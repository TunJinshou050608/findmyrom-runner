"""Device and resource decisions must be grounded in the actual Telegram post."""
import json
import re
from urllib.parse import urlsplit,unquote
from .rule_extractor import extract_package_by_rules,PROJECTS
from .trial import qualification,MAINSTREAM

EXTRA_PROJECTS=[('BlissROM',r'\bBliss\s*ROM\b|#BlissROM'),('Project Elixir',r'Project\s*Elixir'),('Xtended',r'\bXtended\b'),('AlphaDroid',r'AlphaDroid'),('ColtOS',r'ColtOS'),('Bootleggers',r'Bootleggers'),('PixelExperience',r'Pixel\s*Experience|#PEX\b'),('PixelPlusUI',r'Pixel\s*Plus\s*UI'),('AOSPA',r'\bAOSPA\b'),('OxygenOS',r'OxygenOS'),('ColorOS',r'ColorOS'),('OriginOS',r'OriginOS')]
MAINSTREAM_ROMS=MAINSTREAM|{'pixelexperience','pixelplusui','blissrom','project elixir','xtended','alphadroid','coltos','aospa'}


def token(text,value):return bool(re.search(r'(?<![a-z0-9_])'+re.escape(value)+r'(?![a-z0-9_])',text,re.I))


def path_token(text,value):return bool(re.search(r'(?<![a-z0-9])'+re.escape(value)+r'(?![a-z0-9])',text,re.I))


def target_evidence(message,target,names,all_names,profile=''):
    text=message.get('text','');head='\n'.join(text.splitlines()[:4])
    files=(message.get('file_attachment_name') or '')+' '+' '.join(unquote(urlsplit(u).path) for u in message.get('links',[]) if urlsplit(u).hostname not in ('t.me','telegram.me'))
    tokens=re.findall(r'(?i)#([a-z][a-z0-9_-]{2,24})\b|(?:codename|device)\s*[:：-]\s*([a-z0-9_-]{3,24})\b|[\[(]\s*([a-z0-9_-]{3,24})\s*[\])]',text)
    explicit={part.lower() for groups in tokens for part in groups if part and (part.lower() in all_names or part.lower()==target)}
    if explicit and target not in explicit:return None
    if len(explicit)>1:
        if not path_token(files,target):return None
        return {'kind':'target_in_download_path','value':target,'multi_device_post':True}
    if target in explicit:return {'kind':'explicit_codename','value':target}
    if path_token(files,target):return {'kind':'target_in_download_path','value':target}
    for alias in names:
        if len(alias)>4 and token(text,alias):return {'kind':'catalogue_model_alias','value':alias}
    if token(head,target):return {'kind':'codename_in_release_heading','value':target}
    if token(text,target):return {'kind':'codename_in_release_body','value':target}
    # A channel name alone is not evidence. A profile must independently name
    # exactly this model, without another known target/alias in its description.
    if profile and (token(profile,target) or any(token(profile,name) for name in names)):
        other=[code for code,aliases in all_names.items() if code!=target and
               (token(profile,code) or any(len(a)>7 and token(profile,a) for a in aliases))]
        if not other:return {'kind':'dedicated_channel_profile','value':profile[:300]}
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
    package=extract_package_by_rules(message)
    if package is None:return None,'not_a_resource'
    package['device_codename']=target
    if evidence.get('multi_device_post'):
        attachment_target=path_token(package.get('file_attachment_name') or '',target)
        links=[u for u in json.loads(package['download_links_json']) if path_token(unquote(urlsplit(u).path),target) or (attachment_target and u==package['tg_post_url'])]
        if not links:return None,'ambiguous_multi_device_downloads'
        package['download_links_json']=json.dumps(links,ensure_ascii=False)
        if not attachment_target:package.update(file_attachment_name=None,has_tg_attachment=0)
        filename=unquote(urlsplit(links[0]).path).rstrip('/').split('/')[-1]
        if re.search(r'(?i)\.(?:zip|img|tgz|7z)(?:\.gz)?$',filename):package['package_name']=filename
    text=message.get('text','');heading=primary_release(package,message)
    links=json.loads(package['download_links_json'])
    paths=' '.join(unquote(urlsplit(u).path) for u in links)
    stock_filename=bool(re.search(r'(?i)(?:miui_[A-Z0-9]+_|[a-z0-9]+_images_(?:OS\d|V\d))',paths))
    generic_image=bool(re.search(r'(?i)\bGSI\b',heading) or re.search(r'(?i)(?:ErfanGSI|NipponGSI|/Generic[ /%-]|[-_]AB[-_]\d)',paths))
    oem_link=any((urlsplit(u).hostname or '') in ('bigota.d.miui.com','hugeota.d.miui.com','dl.google.com','bkt-sgp-miui-ota-update-alisgp.oss-ap-southeast-1.aliyuncs.com') for u in links)
    project_base=re.sub(r'(?i)(?:rom|os)$','',package['project_name']).strip()
    official=bool(re.search(r'(?i)\bofficial\b',heading) or (len(project_base)>3 and re.search(r'(?i)\b'+re.escape(project_base)+r'\s*(?:ROM|OS)?\s+Official\s+Channel\b',text)))
    if stock_filename or oem_link:label='stock_official'
    elif re.search(r'(?i)\bunofficial\b|#unofficial|\bcommunity\b|^\s*(?:status|build type)\s*[:：-]\s*unofficial',heading):label='unofficial'
    elif re.search(r'(?i)\bport(?:ed)?\b|\bcustom\b',heading):label='custom_or_port'
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
    elif qualification(package)!='package':reason='unresolved_download_reference'
    elif label=='stock_official':reason='excluded_stock_firmware'
    elif label=='mainstream_official':reason='excluded_mainstream_official_rom'
    elif label=='official_project_unclassified':reason='official_rom_scope_unproved'
    elif label=='unknown' and package['resource_kind']=='rom' and package['project_name'].lower() in MAINSTREAM_ROMS:reason='mainstream_release_status_unproved'
    return {'package':package,'evidence':evidence,'classification':label,'accepted':reason=='accepted'},reason
