"""Sony ESS official metadata adapter; internal chunks form ONE complete firmware bundle."""
import hashlib
import json
import re
from urllib.parse import urlencode, urlsplit, unquote
import xml.etree.ElementTree as ET

API = 'https://app.swup.update.sony.net/ess-distribution/public/api'
MAX_XML = 8 * 1024 * 1024


def _http_bytes(http, url):
    resp = http.get(url)
    if isinstance(resp, (bytes, bytearray)):
        return bytes(resp)
    resp.raise_for_status()
    data = resp.content
    if len(data) > MAX_XML:
        raise ValueError('Unsafe Sony metadata')
    return data


def xml(data):
    if len(data) > MAX_XML or b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():
        raise ValueError('Unsafe Sony metadata')
    return ET.fromstring(data)


def api_url(value):
    value = unquote(value)
    p = urlsplit(value)
    if (
        p.scheme != 'https'
        or p.hostname != 'app.swup.update.sony.net'
        or p.port
        or p.username
        or p.password
        or not p.path.startswith('/ess-distribution/public/api/')
    ):
        raise ValueError('Untrusted Sony metadata link')
    return value


def local_tag(tag):
    return tag.rsplit('}', 1)[-1]


def nodes(root, name):
    return [e for e in root.iter() if local_tag(e.tag) == name]


def field(node, name):
    return next(((e.text or '').strip() for e in node.iter() if local_tag(e.tag) == name), '')


def link(node, rel):
    return next((e.get('href', '') for e in node.iter() if local_tag(e.tag) == 'link' and e.get('rel') == rel), '')


def manifest_digest(manifest):
    return hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def validate_manifest(manifest):
    from .oem_sources import download_origin
    files = manifest.get('files', [])
    if not files or len(files) > 20:
        raise ValueError('Invalid Sony component count')
    total = 0
    total_chunks = 0
    names = set()
    for f in files:
        name = f.get('name', '')
        if not re.fullmatch('[A-Za-z0-9._-]{1,180}', name) or name in ('.', '..') or name in names:
            raise ValueError('Invalid Sony filename')
        names.add(name)
        chunks = f.get('chunks', [])
        if not chunks or len(chunks) > 256:
            raise ValueError('Invalid Sony chunk count')
        total_chunks += len(chunks)
        if total_chunks > 400:
            raise ValueError('Sony total chunk count exceeds streaming budget')
        start = chunks[0]['number']
        if start not in (0, 1) or [c['number'] for c in chunks] != list(range(start, start + len(chunks))):
            raise ValueError('Sony chunk sequence gap')
        for c in chunks:
            if type(c['size']) is not int or c['size'] <= 0 or c['size'] > 256 * 1024 * 1024 or download_origin('sony', c['url']) != 'oem':
                raise ValueError('Untrusted Sony chunk')
        if sum(c['size'] for c in chunks) != f['size']:
            raise ValueError('Sony component size mismatch')
        total += f['size']
    if total > 16 * 1024**3:
        raise ValueError('Complete Sony bundle exceeds streaming archive limit')
    return total


def releases(device, http):
    config = device['sony']
    rows = []
    errors = []
    prefer = ['SOFTWARE_UPDATE_CONTENT_ERASE', 'CR_SOFTWARE_UPDATE', 'SOFTWARE_UPDATE']
    for cust in config['custs']:
        try:
            query = {
                'CDFId': cust['id'],
                'HwSetupKey': 'Default',
                'HWVariantId': config['hwid'],
                'ModelObjectId': config['mid'],
                'ProductObjectId': config['pid'],
                'SecurityStateType': 'COMMERCIAL',
            }
            if cust.get('spc'):
                query['SonyProductCode'] = cust['spc']
            root = xml(_http_bytes(http, API + '/device-service/match/v2?' + urlencode(query)))
            services = []
            for node in nodes(root, 'software-device-service-info'):
                version = field(node, 'software-version')
                logic = field(node, 'logic-type')
                url = link(node, 'self')
                if version and logic and url:
                    services.append({'version': version, 'logic': logic, 'url': api_url(url)})
            if not services:
                raise ValueError('No Sony firmware service')
            service = min(services, key=lambda s: prefer.index(s['logic']) if s['logic'] in prefer else 99)
            listing = xml(_http_bytes(http, service['url']))
            files = []
            for node in nodes(listing, 'file-resource'):
                chunks_xml = xml(_http_bytes(http, api_url(link(node, 'file-chunk-info'))))
                chunks = []
                for chunk in nodes(chunks_xml, 'file-chunk'):
                    url = link(chunk, 'download')
                    host = (urlsplit(url).hostname or '').lower()
                    if url.startswith('http://') and (
                        host == 'sonymobile.com'
                        or host.endswith('.sonymobile.com')
                        or host == 'sony.net'
                        or host.endswith('.sony.net')
                    ):
                        url = 'https://' + url[7:]
                    chunks.append({'number': int(chunk.get('number', '0')), 'size': int(field(chunk, 'size')), 'url': url})
                chunks.sort(key=lambda c: c['number'])
                files.append({
                    'key': field(node, 'file-key'),
                    'name': field(node, 'name'),
                    'size': int(field(node, 'plain-size')),
                    'chunks': chunks,
                })
            manifest = {
                'model': device['codename'],
                'cust': cust['name'],
                'cdf': cust['id'],
                'version': service['version'],
                'service': service['url'],
                'files': files,
            }
            total = validate_manifest(manifest)
            key = manifest_digest(manifest)
            rows.append({
                'vendor': 'sony',
                'device_id': device['upstream_id'],
                'version': service['version'],
                'region': (cust['name'] or 'Customization')[:40] + ' · CDF ' + cust['id'],
                'kind': 'sony_bundle',
                'android_version': 'unknown',
                'size': f'{total / 1024**3:.2f} GiB · complete firmware TAR',
                'bundle': manifest,
                'download_url': 'https://findmyrom.com/sony/bundles/' + key,
                'origin_kind': 'sony_assembled_bundle',
                'resolution': 'resolved',
            })
        except Exception as exc:
            if getattr(exc, 'fmr_batch_deadline', False):
                raise
            errors.append({'cdf': cust['id'], 'error': type(exc).__name__, 'reason': str(exc)[:180]})
    return device, rows, {
        'provider': 'sony_ess',
        'errors': errors,
        'coverage': 'currently offered customization services, not withdrawn firmware history',
    }
