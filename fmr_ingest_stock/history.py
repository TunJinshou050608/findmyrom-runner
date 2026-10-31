"""Bounded full-ROM history discovery. Never fetches a firmware body.

The upstream index is provenance, not a claim that withdrawn/private builds are
observable. Every missing archive, failed detail page and conflicting package is
preserved in the coverage manifest, rather than silently dropped.
"""
from concurrent.futures import ThreadPoolExecutor
import hashlib
from html.parser import HTMLParser
import re
import time
from urllib.parse import unquote, urljoin, urlsplit
import httpx
from .firmware_sync import normalize

INDEX = 'https://xmfirmwareupdater.com'
LABELS = ['Device', 'Codename', 'Version', 'Branch', 'Type', 'Size',
          'Release Date', 'Package Name', 'MD5', 'Download', 'Changelog']


class Document(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links, self.groups, self.stack = [], [], []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'a' and attrs.get('href'):
            self.links.append(attrs['href'])
        if tag == 'ul':
            self.stack.append({'text': [], 'urls': []})
        urls = []
        if tag == 'button' and attrs.get('id') == 'download':
            urls.extend(re.findall(r"window\.open\(['\"]([^'\"]+)['\"]", attrs.get('onclick', '')))
        if tag == 'a' and attrs.get('href', '').lower().split('?')[0].endswith(('.zip', '.tgz', '.tar.gz')):
            urls.append(attrs['href'])
        for group in self.stack:
            group['urls'].extend(urls)

    def handle_data(self, data):
        for group in self.stack:
            group['text'].append(data)

    def handle_endtag(self, tag):
        if tag == 'ul' and self.stack:
            self.groups.append(self.stack.pop())


def field(text, label):
    end = '|'.join(re.escape(x) + r'\s*:' for x in LABELS)
    match = re.search(re.escape(label) + r'\s*:\s*(.*?)(?=\s+(?:' + end + r')|$)', text)
    return match[1].strip() if match else ''


def official(url):
    p = urlsplit(url)
    return (p.scheme == 'https' and not p.username and not p.password and not p.port
            and bool(p.hostname) and (p.hostname == 'd.miui.com' or p.hostname.endswith('.d.miui.com')))


def model_code(code, version):
    for marker, suffix in [('MIXM','global'),('EUXM','eea'),('INXM','in'),('IDXM','id'),
                           ('RUXM','ru'),('TRXM','tr'),('TWXM','tw'),('JPXM','jp')]:
        if marker in version.upper():
            return code + '_' + suffix
    return code


def parse_detail(html, code):
    doc = Document()
    doc.feed(html)
    rows, rejected = [], 0
    for group in doc.groups:
        text = ' '.join(' '.join(group['text']).split())
        if 'Package Name:' not in text:
            continue
        version, filename, kind = field(text, 'Version'), field(text, 'Package Name'), field(text, 'Type')
        if field(text, 'Codename').split('_', 1)[0].lower() != code:
            rejected += 1
            continue
        links = sorted({u for u in group['urls'] if official(u)
                        and unquote(urlsplit(u).path).rsplit('/', 1)[-1] == filename},
                       key=lambda u: ('bigota.d.miui.com' not in u, u))
        if not links or kind not in {'Recovery', 'Fastboot'} or 'blockota' in filename.lower() or filename.lower().startswith('fw_'):
            rejected += 1
            continue
        android = re.search(r'(?:_|-)(\d{1,2}\.0)(?:_|-|\.)', filename)
        row = {'codename': model_code(code, version), 'version': version,
               'android': android[1] if android else 'unknown', 'link': links[0],
               'md5': field(text, 'MD5').lower(), 'date': field(text, 'Release Date'),
               'size': field(text, 'Size'), 'branch': field(text, 'Branch')}
        try:
            item = normalize(row, 'xiaomi')
            if item['device_codename'] != code or not re.fullmatch('[0-9a-f]{32}', row['md5']):
                raise ValueError('Invalid record')
            if (kind == 'Recovery') != (item['package_type'] == 'recovery_zip'):
                raise ValueError('Package kind mismatch')
        except ValueError:
            rejected += 1
            continue
        rows.append(row)
    return rows, rejected


def bounded_html(client, url):
    p = urlsplit(url)
    if p.scheme != 'https' or p.netloc != urlsplit(INDEX).netloc:
        raise ValueError('Untrusted history origin')
    for attempt in range(3):
        try:
            with client.stream('GET', url) as response:
                response.raise_for_status()
                data = bytearray()
                for chunk in response.iter_bytes(65536):
                    data.extend(chunk)
                    if len(data) > 2 * 1024 * 1024:
                        raise ValueError('History page exceeds bound')
            return bytes(data)
        except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as exc:
            if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code < 500 and exc.response.status_code != 429:
                raise
            if attempt == 2:
                raise
            time.sleep(attempt + 1)


def discover_device(code, client=None):
    if not re.fullmatch('[a-z0-9-]{2,32}', code):
        raise ValueError('Invalid target')
    owned = client is None
    client = client or httpx.Client(timeout=30, follow_redirects=False, headers={'User-Agent': 'FindMyROM-History'})
    manifest = {'archive_pages': [], 'detail_pages': [], 'errors': [], 'conflicts': [],
                'rejected_records': 0, 'discovered_pages': 0}
    versions, seen = set(), set()
    archives = [INDEX + '/archive/' + family + '/' + code + '/' for family in ('miui', 'hyperos')]
    try:
        while archives:
            url = archives.pop(0)
            if url in seen:
                continue
            if len(seen) >= 100:
                manifest['errors'].append({'stage': 'archive', 'error': 'PaginationLimit'})
                break
            seen.add(url)
            try:
                data = bounded_html(client, url)
            except httpx.HTTPStatusError as exc:
                manifest['archive_pages'].append({'url': url, 'status': exc.response.status_code})
                if exc.response.status_code != 404:
                    manifest['errors'].append({'stage': 'archive', 'error': type(exc).__name__})
                continue
            except Exception as exc:
                manifest['errors'].append({'stage': 'archive', 'error': type(exc).__name__})
                continue
            manifest['archive_pages'].append({'url': url, 'status': 200, 'sha256': hashlib.sha256(data).hexdigest()})
            doc = Document()
            doc.feed(data.decode('utf-8'))
            for href in doc.links:
                absolute = urljoin(url, href)
                p = urlsplit(absolute)
                if p.netloc != urlsplit(INDEX).netloc:
                    continue
                path = unquote(p.path)
                if re.fullmatch(r'/(miui|hyperos)/' + re.escape(code) + r'/[^/]+/[^/]+/', path):
                    versions.add(absolute.split('#')[0])
                if path in {'/archive/miui/'+code+'/', '/archive/hyperos/'+code+'/'} and p.query:
                    archives.append(absolute.split('#')[0])
        manifest['discovered_pages'] = len(versions)
        def fetch(url):
            try:
                data = bounded_html(client, url)
                rows, rejected = parse_detail(data.decode('utf-8'), code)
                return url, rows, rejected, hashlib.sha256(data).hexdigest(), None
            except Exception as exc:
                return url, [], 0, None, type(exc).__name__
        records = []
        with ThreadPoolExecutor(max_workers=4) as pool:
            for url, rows, rejected, digest, error in pool.map(fetch, sorted(versions)):
                manifest['detail_pages'].append({'url': url, 'sha256': digest, 'records': len(rows)})
                manifest['rejected_records'] += rejected
                if error or not rows:
                    manifest['errors'].append({'stage': 'detail', 'url': url, 'error': error or 'NoFullROMRecord'})
                records.extend(rows)
        unique = {}
        for row in sorted(records, key=lambda r: ('.debug' in r['link'], r['link'])):
            n = normalize(row, 'xiaomi')
            key = tuple(n[k] for k in ('device_codename','region_code','version_str','package_type'))
            if key in unique and unique[key]['link'] != row['link']:
                # The frozen schema admits one package per natural key. Preserve
                # every alternate in the private manifest; never claim it was published.
                manifest['conflicts'].append({'key': list(key), 'alternate': row})
            else:
                unique[key] = row
        if not unique:
            manifest['errors'].append({'stage': 'coverage', 'error': 'NoHistoryFound'})
        return sorted(unique.values(), key=lambda r: (r['date'], r['version'], r['link']), reverse=True), manifest
    finally:
        if owned:
            client.close()
