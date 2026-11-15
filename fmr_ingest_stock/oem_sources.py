"""Official OEM host validation and canonical device code helpers."""
from __future__ import annotations

import hashlib
import re
from urllib.parse import SplitResult, urlsplit

from fmr_core.extraction_policy import official_cdn_hosts


def safe_url(url: str) -> SplitResult:
    """Validate that `url` is a clean HTTPS URL without credentials, port, or control chars."""
    p = urlsplit(url)
    if (
        p.scheme != 'https'
        or not p.hostname
        or p.username
        or p.password
        or p.port
        or any(ord(c) < 33 for c in url)
    ):
        exc = ValueError('Unsafe catalogue URL')
        exc.catalogue_url_issues = [  # type: ignore[attr-defined]
            name
            for name, condition in (
                ('scheme', p.scheme != 'https'),
                ('host', not p.hostname),
                ('credentials', bool(p.username or p.password)),
                ('port', bool(p.port)),
                ('whitespace', any(ord(c) < 33 for c in url)),
            )
            if condition
        ]
        raise exc
    return p


def download_origin(vendor: str, url: str) -> str | None:
    """Return `'oem'` if `url` belongs to an official OEM CDN host for `vendor`, else `None`."""
    p = safe_url(url)
    host = (p.hostname or '').lower()
    brand = str(vendor or '').strip().lower()
    if host == 'android.googleapis.com':
        if brand in {'oneplus', 'oppo', 'realme', 'nothing', 'motorola', 'tecno', 'infinix', 'itel'} and re.fullmatch(
            r'/packages/ota-api/[A-Za-z0-9/_.-]+\.zip', p.path
        ):
            return 'oem'
        return None
    for root in official_cdn_hosts(brand):
        root_lc = root.lower()
        if host == root_lc or host.endswith('.' + root_lc):
            return 'oem'
    return None


def canonical_code(vendor: str, upstream: str) -> str:
    """Build deterministic canonical device codename (<= 32 chars)."""
    value = re.sub(r'[^a-z0-9-]+', '-', upstream.lower()).strip('-')
    if not value:
        raise ValueError('Missing device identity')
    result = value if vendor == 'xiaomi' else f'{vendor}-{value}'
    if len(result) > 32:
        digest = hashlib.sha256(f'{vendor}:{upstream}'.encode()).hexdigest()[:10]
        result = f"{result[:21].rstrip('-')}-{digest}"
    return result
