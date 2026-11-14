"""Versioned public metadata. Never changes the download identity or refresh source."""
import re
from urllib.parse import unquote, urlsplit

REVISION = 1
# Revision 3 was retired as equivalent to 2. Reserve it permanently;
# the next semantic normalization change must use revision 4 or higher.
NORMALIZATION_REVISION = 2


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


def system_fields(value, vendor=None):
    raw=str(value or '').strip()
    android=re.match(r'^(?:Android\s+)?(\d+(?:\.\d+)*)(?=\s*\(|$)',raw,re.I)
    if vendor == 'nothing' and android and not raw.lower().startswith('android '):
        return {'android_version':'unknown','os_raw':raw,'os_name':'Nothing OS','os_version':android[1]}
    os_match=re.match(r'(ColorOS|OxygenOS|Funtouch OS|OriginOS|HyperOS|MIUI|One UI|Nothing OS|realme UI)(?:\s+|(?=\d)|$)(.*)',raw,re.I)
    return {'android_version':android[1] if android else 'unknown','os_raw':raw,
            'os_name':os_match[1] if os_match else None,'os_version':os_match[2] or None if os_match else None}


def google_identity(device, release):
    """Use the official archive build, independent of carrier/date order in captions."""
    raw=str(release['version']).strip()
    filename=release.get('filename') or unquote(urlsplit(release.get('download_url') or '').path.rsplit('/',1)[-1])
    model=str(device.get('codename') or device['upstream_id'])
    archive=re.fullmatch(re.escape(model)+r'-(?:ota-)?([a-z0-9._-]+?)-(?:factory-)?[a-f0-9]{8,64}\.zip',filename,re.I)
    block=re.search(r'\(([^)]+)\)',raw)
    parts=[p.strip() for p in block[1].split(',')] if block else []
    build=archive[1].upper() if archive else next((p.upper() for p in parts if re.fullmatch(r'[A-Za-z][A-Za-z0-9._-]*\d[A-Za-z0-9._-]*',p) and not re.search(r'\s',p)),raw)
    # Historical simple build captions are already canonical.
    channels=[p for p in parts if p.upper()!=build and not re.fullmatch(r'(?:[A-Za-z]{3,9}\.?\s+\d{4}|\d{4}-\d{2}(?:-\d{2})?)',p)]
    region=str(release.get('region') or 'Global')
    if region=='Global' and channels:region=' · '.join(channels)[:64]
    return build,region,channels


def normalize(device, release):
    vendor = device['vendor']
    if vendor=='nothing' and release.get('os_raw'):
        inferred=system_fields(release['os_raw'],vendor)
        if inferred.get('os_name')=='Nothing OS':
            release={**release,'os_name':inferred['os_name'],'os_version':inferred['os_version']}
    raw = str(release['version']).strip()
    model = str(device.get('codename') or device['upstream_id'])
    if vendor=='xiaomi' and release.get('variant_id'):
        from .xiaomi_variants import model_key
        model=model_key(model,release['variant_id'])
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
        display, _, channels = google_identity(device, release)
        fields['channels'] = channels
    elif vendor == 'sony':
        fields['cdf'] = str(release.get('bundle', {}).get('cdf') or '')
        fields['customization'] = str(release.get('bundle', {}).get('cust') or '')
    value = clean(release.get('android_version'))
    android = re.fullmatch(r'(?:Android\s+)?(\d+(?:\.\d+)*)(?:\s*\([^)]*\))?', value, re.I)
    if vendor == 'google' and not android:
        android = re.match(r'^(\d+(?:\.\d+)*)(?=\s|\(|$)', value)
    os_match = re.match(r'(ColorOS|OxygenOS|Funtouch OS|OriginOS|HyperOS|MIUI|One UI|Nothing OS|realme UI)(?:\s+|(?=\d)|$)(.*)', clean(release.get('os_raw')) or value, re.I)
    if vendor == 'nothing' and clean(release.get('os_name')) == 'Nothing OS' and value == clean(release.get('os_version')):
        android = None
    official = release.get('_official_metadata') or {}
    # An official record must match both the actual model and requested sales code.
    if official.get('model') != model or official.get('csc') != release.get('region') or official.get('ap') != display:
        official = {}
    android_version = official.get('android') or (android[1] if android else None)
    filename = release.get('filename') or release.get('refresh_source', {}).get('filename')
    if not filename and release.get('download_url') and not release.get('refresh_source') and vendor != 'sony':
        filename = unquote(urlsplit(release['download_url']).path.rsplit('/', 1)[-1])
    source = release.get('metadata_source') or release.get('source_reference') or device.get('page') or release.get('bundle',{}).get('service')
    if source and vendor == 'sony':
        p=urlsplit(source);source=p._replace(query='',fragment='').geturl()
    result = {'schema': REVISION, 'vendor': vendor, 'model': model, 'raw_version': raw,
              'display_version': display, 'region': google_identity(device,release)[1] if vendor == 'google' else release.get('region') or 'Global',
              'android_version': android_version, **({'variant_label':release.get('variant_label')} if vendor=='xiaomi' and release.get('variant_id') else {}), 'os_name': os_match[1] if os_match else clean(release.get('os_name')) or None,
              'os_version': os_match[2] or None if os_match else clean(release.get('os_version')) or None,
              'published_date': iso_date(official.get('date') or release.get('date')),
              'security_patch': iso_date(official.get('patch') or release.get('security_patch')), 'original_filename': filename,
              'file_size': clean(release.get('size'))[:256] or None,
              'vendor_fields': fields, 'sources': {'catalogue': source, 'official_update': official.get('source')}}
    if vendor=='xiaomi' and release.get('availability')=='unavailable':
        result.update(availability='unavailable',unavailable_reason=release.get('unavailable_reason') or 'UpstreamUnavailable')
    return result

def package_label(vendor, kind):
    if kind=='recovery_zip':return 'Recovery / OTA ZIP' if vendor=='xiaomi' else 'Full OTA ZIP' if vendor=='google' else 'OTA / update ZIP'
    return {'factory_zip':'Factory image','fastboot_tgz':'Fastboot ROM','fastboot_zip':'Fastboot ROM','incremental_zip':'Incremental OTA','recovery_ozip':'OTA OZIP','samsung_encrypted':'Samsung firmware (encrypted archive)','samsung_zip':'Samsung firmware','sony_bundle':'Sony complete firmware ZIP'}.get(kind,kind)
