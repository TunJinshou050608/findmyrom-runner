"""Deterministic canonicalization for grounded TG extraction values.

The model is good at finding facts and bad at keeping field roles stable. This
module fixes the observed role/format drift without guessing semantics: it only
rewrites a value when another value in the same extraction (or a same-post title
or build line) proves the correct role.
"""
import re

_TRAILING_VERSION = re.compile(r'\s+(v?\d+(?:\.\d+)*)\s*$', re.I)
_ANDROID_PREFIX = re.compile(r'^\s*android\s*', re.I)
_ANDROID_FROM_TEXT = re.compile(r'\bAndroid\s*(\d+(?:\.\d+)*[A-Za-z]?)', re.I)
_ANDROID_SHORT = re.compile(r'\bA(\d{2})\b')
_BRAND_WORDS = re.compile(
    r'\b(?:redmi|xiaomi|poco|samsung|galaxy|oneplus|oppo|vivo|realme|motorola|sony|google|pixel|honor|huawei)\b',
    re.I,
)
_ALIASES = {
    'havocos': 'HavocOS',
    'lineageos': 'LineageOS',
    'crdroid': 'crDroid',
    'pixelexperienceplus': 'PixelExperiencePlus',
    'pixelexperience': 'PixelExperience',
    'aexoreo': 'Aex Oreo',
    'dotos': 'DotOS',
    'arrowos': 'Arrow OS',
    'octavios': 'OctaviOS',
}


def _value(node):
    if isinstance(node, dict):
        return node.get('value')
    return node


def _quote(node):
    if isinstance(node, dict):
        return node.get('evidence_quote')
    return None


def _text(value):
    return str(value).strip() if value is not None else ''


def _norm(value):
    return ' '.join(_text(value).split()).casefold()


def _alpha(value):
    return re.sub(r'[^a-z0-9]', '', _norm(value))


def _grounded(value, text):
    if not _text(value) or not text:
        return False
    return _norm(value) in _norm(text) or _alpha(value) in _alpha(text)


def split_project_name(value):
    """Return (canonical project name, version embedded in the name)."""
    name = _text(value)
    if not name:
        return None, None
    match = _TRAILING_VERSION.search(name)
    version = None
    if match:
        version = match.group(1)
        name = name[:match.start()].strip()
    key = re.sub(r'[^a-z0-9]', '', name.casefold())
    return _ALIASES.get(key, name) or None, version


def canonical_android(value, text):
    value = _text(value)
    if not value:
        match = _ANDROID_FROM_TEXT.search(text)
        if match:
            value = match.group(1)
        else:
            # Only trust a short A12/A13 token in the title line. Hashtags such
            # as #A64 are architecture/version tags, not the Android version.
            match = _ANDROID_SHORT.search(text.splitlines()[0] if text else '')
            if match:
                value = match.group(1)
    value = _ANDROID_PREFIX.sub('', value).strip()
    match = re.fullmatch(r'A(\d{2})', value, re.I)
    if match:
        value = match.group(1)
    if not value or value.casefold() in ('unknown', 'null', 'none'):
        return None
    if _BRAND_WORDS.search(value):
        return None
    return value


def canonical_project_version(value, embedded, android, text):
    value = _text(value)
    if embedded:
        return embedded
    if not value:
        return None
    if value.casefold() in ('unknown', 'null', 'none'):
        return None
    if re.match(r'^android\b', value, re.I):
        return None
    if android and value.casefold() == android.casefold():
        return None
    lines = text.splitlines()
    title = '\n'.join(lines[:3])
    build = '\n'.join(line for line in lines if re.search(r'build|version|release', line, re.I))
    notes = '\n'.join(line for line in lines if re.search(r'notes?|vendor|firmware', line, re.I))
    if value not in title and value not in build and value in notes:
        return None
    return value


def canonical_devices(codenames, marketing, target, names, text):
    codes = []
    seen = set()
    for code in codenames or []:
        code = _text(code).lower().lstrip('@')
        if not code or ' ' in code or code in seen:
            continue
        seen.add(code)
        codes.append(code)
    names_out = []
    known_codes = {str(code).casefold() for code in names}
    seen_names = {code.casefold() for code in codes}
    for name in marketing or []:
        name = _text(name).lstrip('@')
        if not name or name.casefold() in seen_names or name.casefold() in known_codes:
            continue
        seen_names.add(name.casefold())
        names_out.append(name)
    for alias in names.get(target, []):
        alias = _text(alias)
        if alias and alias.casefold() not in seen_names and _grounded(alias, text):
            seen_names.add(alias.casefold())
            names_out.append(alias)
    return codes, names_out


def canonical_handle(value):
    value = _text(value)
    if not value or ' ' in value or value.count('@') > 1:
        return None
    if not re.fullmatch(r'@?[A-Za-z][A-Za-z0-9_]{3,31}', value):
        return None
    return value


def _set_node(out, field, value, old_quote, text):
    if value in (None, ''):
        out[field] = {'value': None, 'evidence_quote': None}
        return
    quote = old_quote if isinstance(old_quote, str) and old_quote.strip() else value
    out[field] = {'value': value, 'evidence_quote': quote}


def normalize_legacy(out, text, target, names):
    """Canonicalize a legacy {value, evidence_quote} extraction in place."""
    if not isinstance(out, dict) or out.get('is_release') is not True:
        return out

    original_version = _value(out.get('project_version'))
    original_android = _value(out.get('android_version'))
    project_name, embedded_version = split_project_name(_value(out.get('project_name')))
    android = canonical_android(original_android, text)
    project_version = canonical_project_version(
        original_version, embedded_version, android, text
    )

    devices = out.get('target_devices') or {}
    codenames, marketing = canonical_devices(
        devices.get('codenames') or [], devices.get('marketing_names') or [], target, names, text
    )

    maintainer = out.get('maintainer') or {}
    handle = canonical_handle(maintainer.get('handle'))
    display = maintainer.get('display_name')
    if isinstance(display, list):
        display = None
    display = _text(display) or None

    _set_node(out, 'project_name', project_name, _quote(out.get('project_name')), text)
    _set_node(out, 'project_version', project_version, _quote(out.get('project_version')), text)
    _set_node(out, 'android_version', android, _quote(out.get('android_version')), text)
    out['target_devices'] = {
        'codenames': codenames,
        'marketing_names': marketing,
        'evidence_quote': devices.get('evidence_quote') or (codenames[0] if codenames else (marketing[0] if marketing else None)),
    }
    out['maintainer'] = {
        'handle': handle,
        'display_name': display,
        'evidence_quote': maintainer.get('evidence_quote') or handle or display,
    }
    previous = out.get('_normalization') or {}
    original_version_text = _text(original_version)
    clear_project_version = bool(previous.get('clear_project_version')) or (
        project_version is None
        and bool(original_version_text)
        and bool(re.match(r'^android\b', original_version_text, re.I))
    )
    clear_android = bool(previous.get('clear_android')) or (
        android is None
        and bool(_text(original_android))
        and bool(_BRAND_WORDS.search(_text(original_android)))
    )
    out['_normalization'] = {
        'project_name': project_name,
        'embedded_version': embedded_version,
        'project_version': project_version,
        'android_version': android,
        'target_codenames': codenames,
        'target_marketing_names': marketing,
        'maintainer_handle': handle,
        'target_in_codenames': target in codenames,
        'target_in_marketing': any(_grounded(alias, text) for alias in names.get(target, [])),
        'clear_project_version': clear_project_version,
        'clear_android': clear_android,
    }
    return out
