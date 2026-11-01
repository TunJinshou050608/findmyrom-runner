"""Stable, content-addressed descriptors for expiring official download links."""
import hashlib
import json
import re
from urllib.parse import parse_qs, unquote, urlsplit

PREFIX = 'https://findmyrom.com/stock/refresh/'
VENDORS = {'oneplus', 'oppo', 'realme'}
VOLATILE = {'expires', 'token', 'signature', 'sign', 'x-amz-signature',
            'x-amz-expires', 'x-goog-signature', 'x-goog-expires',
            'x-oss-signature', 'x-oss-expires', 'auth_key', 'authkey'}


def expiring(url):
    return bool({key.lower() for key in parse_qs(urlsplit(url).query, keep_blank_values=True)} & VOLATILE)


def validate(source):
    if set(source) != {'schema', 'vendor', 'version', 'landing_page', 'filename'}:
        raise ValueError('Invalid refresh descriptor fields')
    if source['schema'] != 1 or source['vendor'] not in VENDORS:
        raise ValueError('Unsupported refresh descriptor')
    landing = source['landing_page']
    if not isinstance(landing, str) or not re.fullmatch(r'https://fwatlas\.com/download/[1-9][0-9]*/[a-z0-9-]+', landing):
        raise ValueError('Invalid refresh landing')
    if not isinstance(source['version'], str) or not re.fullmatch(r'[\x21-\x7e]{1,256}', source['version']):
        raise ValueError('Invalid refresh version')
    if not isinstance(source['filename'], str) or not re.fullmatch(r'[A-Za-z0-9_.()+-]{1,240}\.zip', source['filename'], re.I):
        raise ValueError('Invalid refresh filename')
    return source


def fingerprint(source):
    validate(source)
    return hashlib.sha256(json.dumps(source, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def valid_release(vendor, release):
    source = release.get('refresh_source')
    if not source:
        return False
    return source['vendor'] == vendor and source['version'] == release['version'] and release['download_url'] == PREFIX + fingerprint(source)


def renewable(release, landing):
    source = {'schema': 1, 'vendor': release['vendor'], 'version': release['version'],
              'landing_page': landing,
              'filename': unquote(urlsplit(release['download_url']).path.rsplit('/', 1)[-1])}
    key = fingerprint(source)
    return {**release, 'download_url': PREFIX + key, 'refresh_source': source,
            'origin_kind': 'oem_on_demand', 'resolution': 'resolved', 'filename': source['filename']}
