"""Versioned public metadata. Never changes the download identity or refresh source."""
import re
from urllib.parse import unquote, urlsplit

REVISION = 1


def clean(value):
    value = str(value or '').strip()
    return '' if value.lower() in ('unknown', 'none', 'n/a', '-', 'android') else value


def iso_date(value):
    from datetime import date
    value = str(value or '')[:10]
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return None


def system_fields(value):
    raw=str(value or '').strip()
    android=re.match(r'^(?:Android\s+)?(\d+(?:\.\d+)*)(?=\s*\(|$)',raw,re.I)
    return {'android_version':android[1] if android else 'unknown','os_raw':raw}


def normalize(device, release):
    vendor = device['vendor']
    raw = str(release['version']).strip()
    model = str(device.get('codename') or device['upstream_id'])
    display = raw
    fields = {}
    if vendor == 'samsung':
        from .samsung_fus import normalize_version
        if '/' in raw:
            parts = normalize_version(raw).split('/')
            if all(re.fullmatch('[A-Z0-9]+', p) for p in parts) and all(p.startswith(model.removeprefix('SM-')) for p in parts):
                display = parts[0]
                fields = dict(zip(('ap', 'csc_version', 'cp', 'data_version'), parts))
    elif vendor == 'google':
        match = re.search(r'\(([A-Za-z][A-Za-z0-9._-]+)(?:[,\s)]|$)', raw)
        if match:
            display = match[1]
        block = re.search(r'\(([^)]+)\)', raw)
        if block:
            fields['channels'] = [p.strip() for p in block[1].split(',')[1:]
                                  if p.strip() and not re.fullmatch(r'(?:[A-Za-z]{3,9}\.?\s+\d{4}|\d{4}-\d{2}(?:-\d{2})?)', p.strip())]
    elif vendor == 'sony':
        fields['cdf'] = str(release.get('bundle', {}).get('cdf') or '')
        fields['customization'] = str(release.get('bundle', {}).get('cust') or '')
    value = clean(release.get('android_version'))
    android = re.fullmatch(r'(?:Android\s+)?(\d+(?:\.\d+)*)(?:\s*\([^)]*\))?', value, re.I)
    if vendor == 'google' and not android:
        android = re.match(r'^(\d+(?:\.\d+)*)(?=\s|\(|$)', value)
    os_match = re.match(r'(ColorOS|OxygenOS|Funtouch OS|OriginOS|HyperOS|MIUI|One UI|Nothing OS)(?:\s+|(?=\d)|$)(.*)', clean(release.get('os_raw')) or value, re.I)
    official = release.get('_official_metadata') or {}
    # An official record must match both the actual model and requested sales code.
    if official.get('model') != model or official.get('csc') != release.get('region') or official.get('ap') != display:
        official = {}
    android_version = official.get('android') or (android[1] if android else None)
    filename = release.get('filename') or release.get('refresh_source', {}).get('filename')
    if not filename and release.get('download_url') and not release.get('refresh_source') and vendor != 'sony':
        filename = unquote(urlsplit(release['download_url']).path.rsplit('/', 1)[-1])
    source = release.get('metadata_source') or release.get('source_reference') or device.get('page') or release.get('bundle',{}).get('service')
    result = {'schema': REVISION, 'vendor': vendor, 'model': model, 'raw_version': raw,
              'display_version': display, 'region': release.get('region') or 'Global',
              'android_version': android_version, 'os_name': os_match[1] if os_match else clean(release.get('os_name')) or None,
              'os_version': os_match[2] or None if os_match else clean(release.get('os_version')) or None,
              'published_date': iso_date(official.get('date') or release.get('date')),
              'security_patch': iso_date(official.get('patch') or release.get('security_patch')), 'original_filename': filename,
              'file_size': clean(release.get('size'))[:256] or None,
              'vendor_fields': fields, 'sources': {'catalogue': source, 'official_update': official.get('source')}}
    return result

def package_label(vendor, kind):
    if kind=='recovery_zip':return 'Recovery / OTA ZIP' if vendor=='xiaomi' else 'Full OTA ZIP' if vendor=='google' else 'OTA / update ZIP'
    return {'factory_zip':'Factory image','fastboot_tgz':'Fastboot ROM','fastboot_zip':'Fastboot ROM','incremental_zip':'Incremental OTA','recovery_ozip':'OTA OZIP','samsung_encrypted':'Samsung firmware (encrypted archive)','samsung_zip':'Samsung firmware','sony_bundle':'Sony complete firmware ZIP'}.get(kind,kind)
