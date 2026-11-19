"""Reuse the rendered website directory order instead of inferring device age."""
from itertools import zip_longest
import re
import time
from urllib.parse import urlsplit, unquote

from bs4 import BeautifulSoup
import httpx

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


def read_order(store, names, *, client=None, now=None):
    now = time.time() if now is None else now
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
