"""Evidence-backed release tags. Unknown values are never publication permission."""
import hashlib
import json
import re
from datetime import datetime
from .rule_extractor import PROJECTS, extract_clean_heading
from urllib.parse import urlsplit
from copy import deepcopy

VERSION = 'release-taxonomy-v2'
ACCESS_POLICY = 'tg-default-free-except-elite-v1'
GENERIC = {'custom rom', 'rom', 'kernel', 'recovery', 'firmware', 'boot image', 'resource'}
BASES = {'HyperOS': r'\bhyper\s*os\b', 'MIUI': r'\bmiui\b',
         'One UI': r'\bone\s*ui\b', 'OxygenOS': r'\boxygen\s*os\b',
         'ColorOS': r'\bcolor\s*os\b', 'OriginOS': r'\borigin\s*os\b',
         'AOSP': r'\baosp\b'}
OEM_PROJECTS = {'hyperos','miui','oxygenos','coloros','originos','xiaomi.eu'}
NON_ROM_PROJECTS = {'twrp','orangefox'}


def match(pattern, text):
    found = re.search(pattern, text, re.I | re.M)
    return found.group(0) if found else None


def access_claim(text):
    # Ignore voluntary support requests, but never ignore access/payment gates.
    original = text
    text = re.sub(r'(?i)\b(?:no\s+payment\s+required|no\s+paid\s+(?:access|subscription)\s+required)\b|无需付费|不收费|不需要付费', '', text)
    paid = match(r'\b(?:paid(?:\s+(?:rom|build|release|access))?|premium\s+(?:rom|build|access)|purchase\s+(?:required|to download)|pay(?:ment)?\s+(?:required|to download|to access)|donat(?:ion|e)\s+(?:required|to download|to access|to unlock)|subscription\s+(?:required|only)|(?:not|no)\s+(?:(?:a|for)\s+)?free|free\s+trial|price\s*[:：]\s*\S+|buy\s+(?:this|the)\s+(?:rom|build))\b|付费|收费|购买后|付款后|订阅后|捐赠后|不免费|并非免费|免费试用|售价\s*[:：]?\s*\d+', text)
    if paid:
        return 'paid', paid
    free = match(r'\b(?:free\s+(?:download|release|build|rom|access|of\s+charge)|(?:download|release|build|rom)\s+(?:is\s+)?free|no\s+payment\s+required|gratis)\b|免费(?:下载|提供|发布|使用)?|无需付费|不收费|不需要付费', original)
    if free:
        return 'free', free
    return 'unknown', None


def project_identity(name):
    if not name or name.lower() in GENERIC:
        return {'id': None, 'name': name or None, 'aliases': []}
    for canonical, pattern in PROJECTS:
        if re.fullmatch(pattern, name, re.I) or canonical.lower() == name.lower():
            name = canonical
            break
    slug = re.sub(r'[^a-z0-9]+', '', name.lower())
    return {'id': 'project:' + slug if slug else None, 'name': name, 'aliases': [name]}


def classify(message, package, *, device_names=(), accepted=False, llm_output=None):
    text = message.get('text', '')
    heading = extract_clean_heading(text)
    # Release labels apply to the main resource, not companion install instructions.
    scope = heading + '\n' + '\n'.join(line for line in text.splitlines() if re.match(
        r'(?i)^\s*(?:status|build type|stage|variant|android|version|build|updated|release date|system base|based on|branch)\s*[:：-]', line))
    evidence = {}
    def record(field, value, quote=None, source='rule'):
        evidence[field] = {'value': value, 'source': source if quote else 'unknown', 'evidence': quote}
        return value
    access, quote = access_claim(text)
    record('access', access, quote)
    kind = {'rom':'rom','recovery':'recovery','kernel':'kernel'}.get(package.get('resource_kind'), 'other_tool')
    record('resource_type', kind, heading)
    project = project_identity(package.get('project_name'))
    for canonical, pattern in PROJECTS:
        if canonical == project['name']:
            for found in re.finditer(pattern,heading,re.I):
                if found[0] not in project['aliases']:project['aliases'].append(found[0])
    record('project', project, project['name'] if project['name'] and project['name'].lower() in text.lower() else None, 'registry')
    base = 'unknown'
    for name, pattern in BASES.items():
        quote = match(pattern, heading)
        if quote:
            base = record('system_base', name, quote)
            break
    if base == 'unknown':
        for name, pattern in BASES.items():
            quote = match(r'(?:based\s+on|base\s*[:：-])\s*' + pattern.replace(r'\b', ''), text)
            if quote:
                base = record('system_base', name, quote)
                break
    # Curated project identity supplies a base only for known AOSP ROM projects.
    if base == 'unknown' and kind == 'rom' and project['name'] and any(project['name'] == n for n,_ in PROJECTS) and project['name'].lower() not in OEM_PROJECTS | NON_ROM_PROJECTS:
        base = record('system_base', 'AOSP', project['name'], 'registry')
    record('system_base', base, evidence.get('system_base', {}).get('evidence'), evidence.get('system_base', {}).get('source', 'rule'))
    family = 'pending'
    if kind == 'rom':
        if base == 'AOSP':family = 'aosp_like'
        elif base != 'unknown':
            if match(r'\bport(?:ed)?\b|移植', heading):family = 'manufacturer_port'
            elif match(r'\b(?:mod(?:ified)?|custom|debloated)\b|修改|精简|官改', heading) or project['name'] == 'Xiaomi.eu':family = 'manufacturer_modified'
    record('rom_family', family if kind == 'rom' else None, heading if family != 'pending' else None)
    recognition = 'unknown'
    quote = match(r'\bunofficial\b|非官方', scope)
    if quote:recognition = 'unofficial'
    else:
        quote = match(r'\bofficial\b|官方认证', scope)
        if quote:recognition = 'official'
    record('project_recognition', recognition, quote, 'author_claim')
    stage = 'unknown'
    for value in ('alpha','beta','stable'):
        quote = match(r'\b' + value + r'\b', scope)
        if quote:
            stage = record('release_stage', value, quote, 'author_claim')
            break
    variants = []
    for value, pattern in [('vanilla',r'\bvanilla\b'),('gapps',r'\bgapps\b')]:
        quote = match(pattern, scope)
        if quote:
            variants.append(value)
            record('variant:' + value, value, quote, 'author_claim')
    quote=match(r'\bmicrog\b',scope)
    if quote:
        variants.append('other')
        record('variant:other','other',quote,'author_claim')
    if not variants:variants = ['unknown']
    form_quote = match(r'\bgsi\b|generic\s+system\s+image', heading)
    form = 'gsi' if form_quote else 'unknown'
    if not form_quote and match(r'(?i)#' + re.escape(package.get('device_codename') or '!') + r'\b',text):
        form,form_quote = 'device_specific',package['device_codename']
    record('package_form', form, form_quote)
    excluded = []
    applicable = []
    for name in dict.fromkeys([package.get('device_codename'), *device_names]):
        if not name or len(name) < 3:
            continue
        escaped = re.escape(name)
        if match(r'(?:not\s+for|except|excluding|不支持|不适用|排除)\s*[:：]?\s*' + escaped, text):excluded.append(name)
        elif match(r'(?<![\w])' + escaped + r'(?![\w])',text):applicable.append(name)
    device_id = package.get('device_codename')
    raw_code = device_id
    if device_id and device_id.split('-',1)[0] in ('google','samsung','oneplus','oppo','realme','vivo','asus','nothing','sony','motorola','huawei','honor') and '-' in device_id:
        suffix=device_id.split('-',1)[1]
        raw_code=next((name for name in device_names if name.lower()==suffix),None)
    if form=='unknown' and applicable:
        form=record('package_form','device_specific',applicable[0])
    device = {'device_id':device_id,'codename':raw_code,
              'applicable_models':applicable,'excluded_models':excluded,
              'compatibility_conditions':[line.strip() for line in text.splitlines() if re.search(r'(?i)\b(?:treble|arm64|a/b|a-only|vndk)\b',line)][:10] if form == 'gsi' else []}
    record('devices',device,', '.join(applicable) or package.get('device_codename'),'catalogue_and_post')
    android = package.get('android_version')
    project_version = None
    quote = match(r'\b(?:project\s+version|version|ver)\s*[:：-]\s*v?([\w.-]+)',scope)
    if quote:project_version=re.split(r'[:：-]\s*',quote,maxsplit=1)[-1].lstrip('v')
    if project_version is None:
        found=re.search(r'\bv(\d+(?:\.[\w-]+)+)\b',heading,re.I)
        if found:project_version=found[1]
    build_id = None
    found = re.search(r'(?im)^\s*(?:build\s*(?:id|number|no\.?)|构建编号)\s*[:：-]\s*([\w.-]+)',text)
    if found:build_id=found[1]
    build_date = None
    found = re.search(r'(?im)^\s*(?:build date|date|updated|release date|构建日期)\s*[:：-]\s*(\d{4}[-/]\d{2}[-/]\d{2})\b',text)
    if found:
        try:build_date=datetime.strptime(found[1].replace('/','-'),'%Y-%m-%d').date().isoformat()
        except ValueError:pass
    versions={'android':android,'project_version':project_version,'build_id':build_id,'build_date':build_date}
    for field,value in versions.items():record('versions.'+field,value,str(value) if value and str(value).lower() in text.lower() else None)
    # Model suggestions must cite an exact quote; they never replace stronger rules,
    # access restrictions, device mapping, identity fields, or author-only labels.
    model = llm_output.get('taxonomy',{}) if isinstance(llm_output,dict) else {}
    allowed={'rom_family':{'aosp_like','manufacturer_modified','manufacturer_port','other'},
             'system_base':set(BASES),'access':{'free','paid'}}
    current={'rom_family':family,'system_base':base,'access':access}
    for field, choices in allowed.items():
        item=model.get(field) if isinstance(model,dict) else None
        if not isinstance(item,dict) or not isinstance(item.get('value'),str) or item['value'] not in choices:continue
        value,quote=item['value'],item.get('evidence')
        if not isinstance(quote,str) or not quote or quote not in text:continue
        if current[field] not in ('pending','unknown'):continue
        if field=='access' and access_claim(quote)[0]!=value:continue
        current[field]=record(field,value,quote,'llm_grounded')
    access,family,base=current['access'],current['rom_family'],current['system_base']
    maintainer = package.get('maintainer_handle')
    if maintainer and not re.search(r'(?i)(?:by|maintainer|developer|author|builder|built\s+by)\s*[:：-]?\s*(?:@|https?://t\.me/)' + re.escape(maintainer.lstrip('@')) + r'\b',text):
        maintainer=None
    maintainer_id = 'tg-handle:' + maintainer.lstrip('@').lower() if maintainer else None
    branch_name = None
    found=re.search(r'(?im)^\s*branch\s*[:：-]\s*([^\n]{1,80})',text)
    if found:branch_name=found[1].strip()
    branch_id = None
    if maintainer_id and project['id']:
        branch_id='branch:'+hashlib.sha256(json.dumps([project['id'],maintainer_id,device['device_id'],branch_name],sort_keys=True).encode()).hexdigest()[:24]
    record('maintenance',{'maintainer_id':maintainer_id,'branch_id':branch_id,'branch_name':branch_name},maintainer or branch_name,'observed_credit_and_derived_id')
    if 'release_stage' not in evidence:record('release_stage','unknown')
    record('build_variants',variants,', '.join(evidence['variant:'+v]['evidence'] for v in variants if 'variant:'+v in evidence) or None,'author_claim')
    blockers=[]
    if not accepted:blockers.append('resource_not_accepted')
    if access!='free':blockers.append('access_'+access)
    if excluded:blockers.append('excluded_model_requires_review')
    if form=='gsi':blockers.append('gsi_compatibility_requires_review')
    if llm_output is not None and llm_output.get('accepted') is False:blockers.append('model_disagreement')
    result = {'schema_version':VERSION,'access':access,'resource_type':kind,'devices':device,
            'rom_family':family if kind=='rom' else None,'system_base':base,'project':project,
            'maintenance':{'maintainer_id':maintainer_id,'branch_id':branch_id,'branch_name':branch_name,'identity_verified':False},
            'project_recognition':recognition,'versions':versions,'build_variants':variants,
            'package_form':form,'release_stage':stage,'evidence':evidence,
            'publication':{'eligible':not blockers,'blockers':blockers}}
    return apply_access_policy(result, message=message, package=package)


def elite_source(message, package):
    """Only source identifiers/known brand markers, never a bare 'elite' adjective."""
    for obj in (message, package):
        for field in ('channel_username','original_channel_username'):
            value=obj.get(field) or ''
            if 'elite' in value.lower():return value
        for field in ('original_post_url','forwarded_from_url','tg_post_url'):
            url=obj.get(field) or ''
            parsed=urlsplit(url)
            if parsed.hostname in ('t.me','telegram.me'):
                channel=parsed.path.strip('/').removeprefix('s/').split('/')[0]
                if 'elite' in channel.lower():return url
    text=message.get('text','')
    branded=match(r'\bEliteRom(?:Lite|NG)?\b|@EliteDevelopment\b',text+'\n'+(package.get('package_name') or '')+'\n'+(package.get('project_name') or ''))
    if branded:return branded
    links=[*message.get('links',[]),*re.findall(r'https?://[^\s<>]+',text)]
    for url in links:
        try:host=(urlsplit(url).hostname or '').lower()
        except ValueError:continue
        if host=='elitedevelopment.com.pk' or host.endswith('.elitedevelopment.com.pk'):return url
    return None


def apply_access_policy(taxonomy, *, message=None, package=None):
    """Retag existing classified records without crawling or changing other decisions."""
    if taxonomy.get('schema_version') not in ('release-taxonomy-v1',VERSION):return taxonomy
    result=deepcopy(taxonomy)
    message=message or {};package=package or {}
    elite=elite_source(message,package)
    # A stored verdict may retain source evidence that is absent from the compact payload.
    if not elite and 'excluded_elite_source' in result.get('publication',{}).get('blockers',[]):
        elite=result.get('evidence',{}).get('access',{}).get('evidence') or 'retained_elite_source_verdict'
    access=result.get('access','unknown')
    evidence=result.setdefault('evidence',{})
    if elite:
        if access!='paid':access='unknown'
        evidence['access']={'value':access,'source':'user_policy','evidence':elite}
    elif access=='unknown':
        access='free'
        evidence['access']={'value':'free','source':'user_policy','evidence':ACCESS_POLICY}
    blockers=[b for b in result.get('publication',{}).get('blockers',[]) if not b.startswith('access_') and b!='excluded_elite_source']
    if access!='free':blockers.append('access_'+access)
    if elite:blockers.append('excluded_elite_source')
    result.update(schema_version=VERSION,access=access,access_policy_version=ACCESS_POLICY,
                  publication={'eligible':not blockers,'blockers':blockers})
    return result
