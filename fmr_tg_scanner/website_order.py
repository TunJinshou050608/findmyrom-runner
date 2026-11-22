"""Reuse the rendered website directory order instead of inferring device age."""
from itertools import zip_longest
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import urlsplit, unquote

from bs4 import BeautifulSoup
import httpx
from .device_snapshot import SNAPSHOT_FILENAME, SNAPSHOT_SCHEMA_VERSION

SITE = 'https://findmyrom.com'
KEY = 'website_device_order_v1'
TTL = 24 * 3600
BACKOFF_KEY = 'website_device_order_backoff_v1'
FAILURE_BACKOFFS = (1800, 3600, 7200)
USER_AGENT = 'FindMyROM-TG-DirectorySync/1.0 (+https://findmyrom.com)'


def _parse_retry_after(header: str | None) -> int | None:
    if not header:
        return None
    stripped = header.strip()
    if stripped.isdecimal():
        return int(stripped)
    return None


def paths(html, prefix):
    result = []
    for a in BeautifulSoup(html, 'html.parser').select('main a[href]'):
        url = urlsplit(a['href'])
        if url.netloc and url.netloc != 'findmyrom.com':
            continue
        if url.path.startswith(prefix):
            code = unquote(url.path[len(prefix):]).rstrip('/')
            if re.fullmatch(r'[a-z0-9][a-z0-9_-]{1,63}', code) and code not in result:
                result.append(code)
    return result


def _load_v4_snapshot_order(names) -> tuple[list[str], dict] | None:
    candidates = []
    for env_key in ('FMR_CATALOGUE_STATE_DIR', 'FMR_STAGING_V4_DIR'):
        val = os.environ.get(env_key)
        if val:
            p = Path(val)
            candidates.extend([p / SNAPSHOT_FILENAME, p.parent / 'staging-v4' / SNAPSHOT_FILENAME])
    candidates.extend([
        Path('/var/lib/findmyrom/catalogue-v3') / SNAPSHOT_FILENAME,
        Path('/var/lib/findmyrom/staging-v4') / SNAPSHOT_FILENAME,
    ])
    for path in candidates:
        if not path.is_file():
            continue
        try:
            snap = json.loads(path.read_text(encoding='utf-8'))
            if snap.get('schema_version') != SNAPSHOT_SCHEMA_VERSION:
                continue
            ordered = [c for c in (snap.get('ordered_devices') or []) if c in names]
            if ordered:
                return ordered, {
                    'source': 'v4_device_snapshot',
                    'fetched_at': snap.get('generated_at', time.time()),
                    'snapshot_path': str(path),
                    'stale_cache': False,
                    'refresh_error': None,
                }
        except Exception:
            continue
    return None


def read_order(store, names, *, client=None, now=None):
    now = time.time() if now is None else now
    if client is None and os.environ.get('FMR_TG_LOCAL_RUNTIME') == 'true':
        snap_order = _load_v4_snapshot_order(names)
        if snap_order is not None:
            return snap_order
    cached = store.meta(KEY)
    backoff = store.meta(BACKOFF_KEY)
    error = None
    in_backoff = bool(cached and isinstance(backoff, dict) and float(backoff.get('next_retry_at', 0)) > now)
    if in_backoff:
        error = backoff.get('last_error')
    elif not cached or cached['fetched_at'] + TTL <= now:
        owned = client is None
        client = client or httpx.Client(
            timeout=20,
            follow_redirects=False,
            trust_env=False,
            headers={'User-Agent': USER_AGENT},
        )
        try:
            def html(path):
                with client.stream('GET', SITE + path) as response:
                    response.raise_for_status()
                    if response.headers.get('content-type', '').split(';')[0] != 'text/html':
                        raise ValueError('Website directory must be HTML')
                    body = bytearray()
                    for part in response.iter_bytes():
                        body.extend(part)
                        if len(body) > 2 * 1024 * 1024:
                            raise ValueError('Website directory body limit')
                    return body.decode('utf-8')
            brands = paths(html('/'), '/brands/')
            if not brands or len(brands) > 20:
                raise ValueError('Invalid website brand directory')
            groups = [(brand, paths(html('/brands/' + brand), '/devices/')) for brand in brands]
            if not any(codes for _, codes in groups):
                raise ValueError('Empty website device directory')
            # Each brand preserves the exact page order; brands take turns.
            ordered = list(dict.fromkeys(code for row in zip_longest(*(codes for _, codes in groups)) for code in row if code))
            if not set(ordered).intersection(names):
                raise ValueError('Website directory does not match the local catalogue')
            cached = {'fetched_at': now, 'devices': ordered,
                      'brand_counts': {brand: len(codes) for brand, codes in groups}}
            store.set_meta(KEY, cached)
            if backoff is not None:
                store.set_meta(BACKOFF_KEY, None)
        except Exception as exc:
            if not cached:
                raise RuntimeError('Website order unavailable; no inferred-age fallback') from exc
            error = type(exc).__name__
            failures = int(backoff.get('failures', 0)) + 1 if isinstance(backoff, dict) else 1
            delay = FAILURE_BACKOFFS[min(failures - 1, len(FAILURE_BACKOFFS) - 1)]
            if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
                ra = _parse_retry_after(exc.response.headers.get('Retry-After'))
                if ra is not None:
                    delay = max(delay, min(ra, 7200))
            store.set_meta(
                BACKOFF_KEY,
                {'failures': failures, 'next_retry_at': now + delay, 'last_error': error},
            )
        finally:
            if owned:
                client.close()
    ordered = [code for code in cached['devices'] if code in names]
    if not ordered:
        raise RuntimeError('Cached website order no longer matches the local catalogue')
    return ordered, {'source': 'website_brand_pages', 'fetched_at': cached['fetched_at'],
                     'brand_counts': cached['brand_counts'], 'stale_cache': cached['fetched_at'] + TTL <= now,
                     'refresh_error': error}
