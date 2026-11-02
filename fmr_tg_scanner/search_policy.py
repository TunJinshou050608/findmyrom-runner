"""Device and resource decisions must be grounded in the actual Telegram post."""
import json
import re
from urllib.parse import urlsplit, unquote
from .rule_extractor import (
    extract_package_by_rules, PROJECTS, download_evidence, extract_clean_heading,
    extract_maintainer_info, dynamic_project_name
)
from .llm_fallback import merge, valid_decision
from .trial import MAINSTREAM

EXTRA_PROJECTS = []
DYNAMIC_COMMUNITY_PROJECTS = set()
MAINSTREAM_ROMS = MAINSTREAM | {
    'pixelexperience', 'pixelplusui', 'blissrom', 'project elixir',
    'xtended', 'alphadroid', 'coltos', 'aospa', 'paranoid android'
}
OFFICIAL_MAINSTREAM_CHANNELS = {
    'crdroidupdates', 'lineageos', 'evolutionxofficial', 'evolutionxupdates',
    'blissrom_updates', 'pixelexperience'
}
DICTIONARY_CODENAMES = {
    'air', 'atom', 'blue', 'bomb', 'citrus', 'daisy', 'dew', 'fire', 'fog',
    'gold', 'ice', 'lake', 'land', 'lime', 'merlin', 'mist', 'moon', 'pine',
    'rock', 'ruby', 'sea', 'sky', 'star', 'sweet', 'warm', 'willow'
}
_profile_catalogue = None
_profile_patterns = []
_profile_cache = {}


def token(text, value):
    return bool(re.search(r'(?<![a-z0-9_])' + re.escape(value) + r'(?![a-z0-9_])', text, re.I))


def path_token(text, value):
    return bool(re.search(r'(?<![a-z0-9])' + re.escape(value) + r'(?![a-z0-9])', text, re.I))


def profile_devices(profile, all_names, *, aliases_only=False):
    """Longest catalogue spans win: Pad 6 Pro must not also imply Pad 6."""
    global _profile_catalogue, _profile_patterns, _profile_cache
    if all_names is not _profile_catalogue:
        _profile_catalogue = all_names
        _profile_cache = {}
        _profile_patterns = [
            (code, re.compile(r'(?<![a-z0-9_])' + re.escape(value) + r'(?![a-z0-9_])', re.I), value == code)
            for code, names in all_names.items() for value in [code, *names] if value
        ]
    cache_key = (profile, aliases_only)
    if cache_key in _profile_cache:
        return _profile_cache[cache_key]
    spans = [
        (m.start(), m.end(), code)
        for code, pattern, is_code in _profile_patterns
        if not aliases_only or not is_code for m in pattern.finditer(profile)
    ]
    ordered = sorted(spans, key=lambda x: (x[0], -x[1]))
    accepted = []
    for start, end, code in ordered:
        if any(a <= start and b >= end and (a < start or b > end) for a, b, _ in accepted):
            continue
        accepted.append((start, end, code))
    result = {code for _, _, code in accepted}
    if len(_profile_cache) >= 512:
        _profile_cache = {}
    _profile_cache[cache_key] = result
    return result


def target_evidence(message, target, names, all_names, profile=''):
    text = message.get('text', '')
    lines = text.splitlines()
    head = '\n'.join(lines[:4])
    first = lines[0] if lines else ''
    files = (message.get('file_attachment_name') or '') + ' ' + ' '.join(
        unquote(urlsplit(u).path) for u in message.get('links', []) if urlsplit(u).hostname not in ('t.me', 'telegram.me')
    )
    identity_text = re.sub(
        r'(?im)(?:ported|extracted|taken|based)\s+from\b[^\n]*|^\s*(?:donor|base device|source device)\s*[:：-][^\n]*',
        '', text
    )

    # 1. Structural tags: #tag, device: codename, [codename], (codename)
    tokens = re.findall(
        r'(?i)#([a-z][a-z0-9_-]{2,24})\b|(?:codename|device)\s*[:：-]\s*([a-z0-9_-]{3,24})\b|[\[(]\s*([a-z0-9_-]{3,24})\s*[\])]',
        identity_text
    )
    explicit = {part.lower() for groups in tokens for part in groups if part and (part.lower() in all_names or part.lower() == target)}
    if explicit and target not in explicit:
        return None
    if len(explicit) > 1:
        return {'kind': 'explicit_multi_device_post', 'value': target, 'multi_device_post': True, 'listed_devices': sorted(explicit)}
    if target in explicit:
        return {'kind': 'explicit_codename', 'value': target}

    # 2. Distinct standalone codenames in headline (excluding dictionary words like 'air' or 'star')
    head_tokens = {w.lower() for w in re.findall(r'\b[a-zA-Z0-9_-]{3,24}\b', first)}
    distinct_head_codes = {w for w in head_tokens if (w in all_names or w == target) and w not in DICTIONARY_CODENAMES}
    if distinct_head_codes and target not in distinct_head_codes:
        return None
    if target in distinct_head_codes:
        return {'kind': 'codename_in_release_heading', 'value': target}

    # 3. Target codename in download filename or path
    if path_token(files, target):
        return {'kind': 'target_in_download_path', 'value': target}

    # 4. Donor / base device exclusion
    donors = re.findall(
        r'(?im)(?:ported|extracted|taken|based)\s+from\s*[:：-]?\s*([^\n]+)|^\s*(?:donor|base device|source device)\s*[:：-]\s*([^\n]+)',
        text
    )
    if any(token(part, target) or any(token(part, name) for name in names) for pair in donors for part in pair if part):
        return None

    # 5. Distinct model aliases (e.g. POCO F3, Xiaomi Pad 6 Pro)
    for alias in names:
        if len(alias) > 4 and token(text, alias):
            named = profile_devices(identity_text, all_names, aliases_only=True)
            if named and target not in named:
                return None
            return {'kind': 'catalogue_model_alias', 'value': alias}

    if token(head, target) and target not in DICTIONARY_CODENAMES:
        return {'kind': 'codename_in_release_heading', 'value': target}
    if token(text, target) and target not in DICTIONARY_CODENAMES:
        return {'kind': 'codename_in_release_body', 'value': target}

    # 6. Dedicated channel profile fallback (when post itself doesn't repeat the codename)
    if profile and (token(profile, target) or any(token(profile, name) for name in names)):
        if profile_devices(profile, all_names) == {target}:
            named = profile_devices(identity_text, all_names, aliases_only=True)
            if named and target not in named:
                return None
            if distinct_head_codes and target not in distinct_head_codes:
                return None
            return {'kind': 'dedicated_channel_profile', 'value': profile[:300]}
    return None


def primary_release(package, message):
    """Determine the main release before examining companion recovery/kernel links."""
    text = message.get('text', '')
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    heading = extract_clean_heading(text)
    name = package.get('file_attachment_name') or package.get('package_name') or ''
    release = heading + '\n' + name
    first = lines[0] if lines else ''

    first_rom = any(re.search(pattern, first, re.I) for name_p, pattern in PROJECTS if name_p not in ('TWRP', 'OrangeFox', 'Xiaomi.eu', 'MIUI', 'HyperOS'))
    first_recovery = bool(re.search(r'(?i)\b(?:TWRP|OrangeFox|OFRP|SHRP|PBRP|recovery)\b', first))
    first_kernel = bool(re.search(r'(?i)\b(?:kernel|anykernel)\b', first)) and not bool(re.search(r'(?i)\b(?:rom|ui|os)\b', first))

    if not first_rom and first_recovery:
        kind = 'recovery'
    elif not first_rom and first_kernel:
        kind = 'kernel'
    elif re.search(r'(?i)\b(?:TWRP|OrangeFox|OFRP|SHRP|PBRP|recovery)\b', heading):
        kind = 'recovery'
    elif re.search(r'(?i)\bkernel\b|AnyKernel', release):
        kind = 'kernel'
    elif re.search(r'(?i)(?:^|[/_ -])(?:init_|vendor_)?boot\.img(?:\.gz)?$', name):
        kind = 'boot_image'
    else:
        kind = package.get('resource_kind', 'rom')

    project = package.get('project_name')
    if not project or project in ('Custom ROM', 'Resource', 'Kernel', 'Recovery'):
        dyn = dynamic_project_name(heading, text, first, kind)
        if dyn:
            project = dyn

    if kind == 'recovery':
        if not project or project in ('MIUI', 'HyperOS', 'Custom ROM', 'Resource'):
            project = 'TWRP' if 'twrp' in heading.lower() else ('OrangeFox' if 'orangefox' in heading.lower() or 'ofrp' in heading.lower() else 'Recovery')
    elif kind == 'kernel':
        if not project or project in ('MIUI', 'HyperOS', 'Custom ROM', 'Resource'):
            project = 'Kernel'
    elif kind == 'boot_image':
        project = 'Boot image'
    elif kind == 'firmware':
        if not project:
            project = 'Firmware'
    else:
        if not project:
            project = 'Custom ROM'

    package['project_name'] = project
    package['resource_kind'] = kind
    return heading


def analyse(message, target, names, all_names, profile='', *, llm_callable=None):
    llm_res = None
    channel = (message.get('channel_username') or '').lower()
    if re.search(r'(?i)screenshots?|wallpapers?|photos?|gallery|themes?|memes?|chat|support\b', channel):
        return None, 'not_a_release'
    if profile and re.search(r'(?i)\b(?:screenshots?|wallpapers?|gallery)\s+(?:channel|only|group)\b', profile):
        return None, 'not_a_release'
    evidence = target_evidence(message, target, names, all_names, profile)
    if evidence is None:
        return None, 'wrong_or_unproved_device'
    text = message.get('text', '')
    head = '\n'.join(text.splitlines()[:4])
    head += '\n' + (message.get('file_attachment_name') or '')
    first = text.splitlines()[0].strip() if text else ''

    # 1. Headline conversational sentences / advisories / questions (Non-release forms)
    if re.search(
        r'(?i)^\s*(?:from now|we have|please |note\s*[:：]|notice\s*[:：]|announcement\s*[:：]|psa\s*[:：]|attention|warning\s*[:：]|guide\s*[:：]|how to flash|flashing guide|if (?:anyone|you)|regarding |about the |fyi|heads up|tips?|important\b|changelog for|fixed\b|status update|progress update|delay|postponed|goodbye|farewell|saying goodbye|thank you all|thanks to everyone|leaving all|it was a long journey)\b',
        first
    ):
        return None, 'not_a_release'
    if re.search(r'(?i)^(?:can |could |does |is there |how |anyone |when |why |where |求|请问|有没有|hi |hello |hey |help |pls |please )', head.strip()):
        return None, 'not_a_release'
    if re.search(r'[?？]', first) and not re.search(r'(?i)\b(?:released?|updated?|build|changelog)\b|发布|更新', head):
        return None, 'not_a_release'
    if re.search(r'(?i)security warning|scam alert|malware|phishing', head):
        return None, 'not_a_release'

    # 2. Pure modules / root apps / APKs (Not an Android OS ROM/Kernel/Recovery)
    if re.search(
        r'(?i)\b(?:magisk|kernelsu|lsposed|xposed|launcher|camera|sound|thermal|ui)\s*(?:module|mod)\b|\b(?:apk|icon\s*pack|font\s*pack|boot\s*animation)\b',
        head
    ) and not re.search(r'(?i)\b(?:rom|kernel|recovery)\b', first):
        return None, 'not_a_release'

    # 3. Hardware / system complaints or rants (Not software releases)
    if re.search(r'(?i)\b(?:worst charging|charging solution|battery drain|screen on time|sot discussion|overheating issue)\b', head):
        return None, 'not_a_release'

    resource_signal = bool(
        re.search(r'(?i)\b(?:rom|kernel|recovery|twrp|orangefox|ofrp|boot|fastboot|init_boot|gsi)\b|刷机包|内核|第三方系统', head) or
        any(re.search(pattern, head, re.I) for _, pattern in PROJECTS) or
        re.search(r'\b[A-Za-z0-9_-]+(?:OS|UI|AOSP)\b', head)
    )
    release_signal = bool(
        re.search(r'(?i)\b(?:release|released|build|updated?|download|version|unofficial|official|community|port|changelog|gapps|vanilla)\b|发布|下载|更新|移植|适配', text) or
        message.get('has_tg_attachment')
    )
    if not resource_signal or not release_signal:
        return None, 'not_a_resource'

    package = extract_package_by_rules(message, default_codename=target, post_only=True)
    if package is None:
        return None, 'not_a_resource'
    package['device_codename'] = target
    package['delivery_type'] = 'telegram_message'
    package['target_post_url'] = package['tg_post_url']
    heading = primary_release(package, message)
    links = message.get('links', [])
    paths = ' '.join(unquote(urlsplit(u).path) for u in links)
    stock_filename = bool(re.search(r'(?i)(?:miui_[A-Z0-9]+_|[a-z0-9]+_images_(?:OS\d|V\d))', paths))
    generic_image = bool(re.search(r'(?i)\bGSI\b', heading) or re.search(r'(?i)(?:ErfanGSI|NipponGSI|/Generic[ /%-]|[-_]AB[-_]\d)', paths))
    oem_link = any((urlsplit(u).hostname or '') in (
        'bigota.d.miui.com', 'hugeota.d.miui.com', 'dl.google.com',
        'bkt-sgp-miui-ota-update-alisgp.oss-ap-southeast-1.aliyuncs.com'
    ) for u in links)
    project_base = re.sub(r'(?i)(?:rom|os)$', '', package['project_name']).strip()
    leading_tags='\n'.join(line for line in text.splitlines()[:3] if re.fullmatch(r'\s*(?:#[A-Za-z0-9_-]+\s*)+',line))
    status_text = heading + '\n' + leading_tags + '\n' + re.sub(r'[_-]', ' ', message.get('file_attachment_name') or '')
    official = bool(
        re.search(r'(?i)\bofficial\b', status_text) or
        (len(project_base) > 3 and re.search(r'(?i)\b' + re.escape(project_base) + r'\s*(?:ROM|OS)?\s+Official\s+Channel\b', text)) or
        channel.lower() in OFFICIAL_MAINSTREAM_CHANNELS
    )
    is_custom_or_mod = bool(
        re.search(r'(?i)\b(?:mod|port(?:ed)?|custom|dev|weekly)\b', heading) or
        any(h in paths for h in ['sourceforge.net', 'drive.google.com', 'pixeldrain.com', 'mediafire.com', 'mega.nz'])
    )
    proj_lower = package['project_name'].lower()
    kind = package['resource_kind']

    is_port = bool(re.search(r'(?i)\bport(?:ed)?\b|\bcustom\b', heading + '\n' + text))
    is_stock_template = bool(re.search(r'(?i)new\s+(?:OS|MIUI|HyperOS)\s+(?:fastboot|recovery)\s+update|(?:hyperos|miui)\s*(?:\d+)?\s*version\s*[:=]', text))

    known_project_names = {name.lower() for name, _ in PROJECTS} | {p.lower() for p in EXTRA_PROJECTS}

    if stock_filename or oem_link or is_stock_template:
        label = 'stock_official' if not is_custom_or_mod else 'custom_or_port'
    elif kind in ('kernel', 'recovery', 'boot_image'):
        label = 'community_official' if official else 'unofficial'
    elif generic_image:
        label = 'generic_gsi'
    elif channel.lower() == 'miuiupdatestracker' and not is_custom_or_mod:
        label = 'stock_official'
    elif proj_lower in ('hyperos', 'miui') and not is_port and not is_custom_or_mod:
        label = 'stock_official'
    elif is_port:
        label = 'custom_or_port'
    elif re.search(r'(?i)\bunofficial\b|#unofficial|\bcommunity\b|^\s*(?:status|build type)\s*[:：-]\s*unofficial', status_text):
        label = 'unofficial'
    elif official:
        if proj_lower in MAINSTREAM_ROMS or channel.lower() in OFFICIAL_MAINSTREAM_CHANNELS:
            label = 'mainstream_official'
        elif proj_lower in known_project_names:
            label = 'community_official'
        else:
            label = 'official_project_unclassified'
    else:
        if proj_lower in MAINSTREAM_ROMS:
            if is_custom_or_mod or re.search(r'(?i)\bby\s*@', text):
                label = 'unofficial'
            else:
                label = 'unknown'
        else:
            label = 'community_official'

    credited = package.get('maintainer_handle') or package.get('maintainer_display_name')
    explicit_credit = bool(re.search(r'(?i)(?:by|maintainer|developer|author|builder|built\s+by)\s*[:：-]?\s*(?:https?://t\.me/|@|[a-zA-Z0-9_]{3,32})', text))
    if not explicit_credit and not package.get('maintainer_display_name'):
        package['maintainer_handle'] = None
    signature = message.get('sender_author_signature') or ''
    if package['origin_type'] == 'author_direct' and not (explicit_credit and credited and credited.lstrip('@').lower() in signature.lower()):
        package['origin_type'] = 'unknown'
    if package['origin_type'] == 'repost_aggregator' and package['original_post_url'] == package['tg_post_url']:
        package['origin_type'] = 'unknown'

    reason = 'accepted'
    if generic_image:
        reason = 'generic_image_donor_is_not_target'
    elif re.search(r'(?i)\bengineering\s+ROM\b', heading):
        reason = 'excluded_engineering_firmware'
    elif re.search(r'(?i)security warning|scam alert|malware|phishing', heading) and not re.search(r'(?i)\b(?:release|build|version)\b', heading):
        reason = 'not_a_release'
    elif label == 'stock_official':
        reason = 'excluded_stock_firmware'
    elif label == 'mainstream_official':
        reason = 'excluded_mainstream_official_rom'
    elif label == 'official_project_unclassified':
        reason = 'official_rom_scope_unproved'
    elif label == 'unknown' and package['resource_kind'] == 'rom' and proj_lower in MAINSTREAM_ROMS:
        reason = 'mainstream_release_status_unproved'

    if reason in ('official_rom_scope_unproved', 'mainstream_release_status_unproved') and llm_callable is not None:
        try:
            try:
                llm_res = llm_callable(text, target=target, rule_reason=reason)
            except TypeError:
                llm_res = llm_callable(text)
            if valid_decision(llm_res):
                if llm_res['accepted'] is True:
                    package = merge(package, llm_res, text=text)
                    label = llm_res.get('classification', 'community_official')
                    reason = 'accepted'
                elif llm_res.get('accepted') is False:
                    cls = llm_res.get('classification', label)
                    label = cls
                    if cls == 'stock_official':
                        reason = 'excluded_stock_firmware'
                    elif cls == 'mainstream_official':
                        reason = 'excluded_mainstream_official_rom'
                    elif cls == 'not_a_release':
                        reason = 'not_a_release'
        except Exception:
            pass

    from .taxonomy import classify
    if not valid_decision(llm_res):llm_res = None
    if reason == 'accepted' and llm_res is None and llm_callable is not None:
        try:
            candidate = llm_callable(text, target=target, rule_reason='taxonomy_enrichment')
            if valid_decision(candidate):llm_res = candidate
        except Exception:
            pass
    package['taxonomy'] = classify(message, package, device_names=names, accepted=reason == 'accepted', llm_output=llm_res)
    return {'package': package, 'evidence': evidence, 'classification': label, 'accepted': reason == 'accepted'}, reason
