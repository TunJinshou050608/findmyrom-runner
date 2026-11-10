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
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit
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
    match = re.search(re.escape(label) + r'\s*:(.*?)(?=\s+(?:' + end + r')|$)', text)
    return match[1].strip() if match else ''


def official(url):
    p = urlsplit(url)
    return (p.scheme == 'https' and not p.username and not p.password and not p.port
            and bool(p.hostname) and (p.hostname == 'd.miui.com' or p.hostname.endswith('.d.miui.com')))


def model_code(code, version, filename=''):
    for marker, suffix in [('MIXM','global'),('EUXM','eea'),('INXM','in'),('IDXM','id'),
                           ('RUXM','ru'),('TRXM','tr'),('TWXM','tw'),('JPXM','jp')]:
        if marker in version.upper():
            return code + '_' + suffix
    if 'CNXM' in version.upper():return code
    if re.search(r'(?:_global_images_|_global_|^miui_[^_]*Global_)',filename,re.I):
        return code+'_global'
    return code


def parse_detail(html, code, diagnostics=None):
    diagnostics=[] if diagnostics is None else diagnostics
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
        row = {'codename': model_code(code, version, filename), 'version': version,
               'android': android[1] if android else 'unknown', 'link': links[0],
               'md5': field(text, 'MD5').lower() if field(text,'MD5').lower() not in ('','unknown','none','n/a','-') else None, 'date': field(text, 'Release Date'),
               'size': field(text, 'Size'), 'branch': field(text, 'Branch')}
        try:
            item = normalize(row, 'xiaomi')
            if item['device_codename'] != code or (row['md5'] and not re.fullmatch('[0-9a-f]{32}', row['md5'])):
                raise ValueError('Invalid record')
            if (kind == 'Recovery') != (item['package_type'] == 'recovery_zip'):
                raise ValueError('Package kind mismatch')
        except ValueError:
            diagnostics.append('InvalidFullROMRecord')
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


HISTORY_REVISION = 2
DETAIL_TTL = 30 * 86400


def history_url(url):
    p=urlsplit(url)
    if p.scheme!='https' or p.netloc!=urlsplit(INDEX).netloc or p.username or p.password or any(ord(c)<32 for c in url):
        raise ValueError('Untrusted history origin')
    return urlunsplit((p.scheme,p.netloc,quote(p.path,safe='/%:@!$&\'()*+,;=-._~'),p.query,''))


class HistoryCheckpoint:
    """Parsed source evidence, not another HTML cache. Task-side SQLite only."""
    def __init__(self,store):
        if getattr(store.db,'mode',None)!='sqlite':raise ValueError('History checkpoints must stay local')
        self.store=store
    def key(self,code,url):
        return self.store.prefix+'history:'+code+':'+hashlib.sha256(url.encode()).hexdigest()
    def read(self,code,urls):
        keys={self.key(code,u):u for u in urls}
        return {keys[k]:v for k,v in self.store.get_many(list(keys)).items() if k in keys}
    def save(self,code,url,value):self.store.put(self.key(code,url),value)


def conflict_kind(first,other):
    a=unquote(urlsplit(first['link']).path).rsplit('/',1)[-1]
    b=unquote(urlsplit(other['link']).path).rsplit('/',1)[-1]
    if a==b:
        return ('checksum_conflict',True) if first.get('md5') and other.get('md5') and first['md5']!=other['md5'] else ('mirror_alias',False)
    if ('.debug' in a)!=('.debug' in b):return 'debug_variant',False
    return 'package_variant',True


def discover_device(code, client=None, route_code=None, max_workers=4, checkpoint=None, max_details=None, previous=None):
    if type(max_workers) is not int or not 1 <= max_workers <= 4:
        raise ValueError('Invalid discovery concurrency')
    if not re.fullmatch('[a-z0-9-]{2,32}', code):
        raise ValueError('Invalid target')
    route = route_code or code
    if not re.fullmatch('[A-Za-z0-9-]{2,32}',route):raise ValueError('Invalid archive route')
    if max_details is not None and (type(max_details) is not int or not 1<=max_details<=100):raise ValueError('Invalid detail budget')
    owned = client is None
    client = client or httpx.Client(timeout=30, follow_redirects=False, headers={'User-Agent': 'FindMyROM-History'})
    manifest = {'archive_pages': [], 'detail_pages': [], 'errors': [], 'conflicts': [],
                'rejected_records': 0, 'discovered_pages': 0}
    versions, seen = set(), set()
    archives = [INDEX + '/archive/' + family + '/' + route + '/' for family in ('miui', 'hyperos')]
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
                if getattr(exc,'fmr_batch_deadline',False):raise
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
                if re.fullmatch(r'/(miui|hyperos)/' + re.escape(route) + r'/[^/]+/[^/]+/', path):
                    versions.add(history_url(absolute))
                if path in {'/archive/miui/'+route+'/', '/archive/hyperos/'+route+'/'} and p.query:
                    archives.append(history_url(absolute))
        manifest['discovered_pages'] = len(versions)
        now=int(time.time())
        saved=checkpoint.read(code,versions) if checkpoint else {}
        records=[];todo=[];pending=[];retry_at=[]
        for url in sorted(versions):
            entry=saved.get(url,{})
            current=entry.get('revision')==HISTORY_REVISION
            fresh=current and not entry.get('error') and entry.get('checked_at',0)+(86400 if entry.get('review_needed') else DETAIL_TTL)>now
            cooling=current and entry.get('error') and entry.get('next_check',0)>now
            if fresh or cooling:
                records.extend(entry.get('rows',[]))
                manifest['detail_pages'].append({k:entry.get(k) for k in ('url','sha256','records','checked_at','rejected')})
                if cooling:
                    manifest['errors'].append({'stage':'detail','url':url,'error':entry['error'],'status':entry.get('status')})
                    retry_at.append(entry['next_check'])
            else:todo.append(url)
        # Failed pages first; successful, already checkpointed pages never refetch
        # merely because the disposable HTML cache was evicted.
        repair=set()
        for u in (previous or {}).get('pending_priority',[])+[e.get('url') for e in (previous or {}).get('errors',[]) if e.get('url')]:
            try:repair.add(history_url(u))
            except ValueError:continue
        todo.sort(key=lambda u:(not (bool(saved.get(u,{}).get('error')) or u in repair),u))
        if max_details is not None:pending=todo[max_details:];todo=todo[:max_details]
        def fetch(url):
            try:
                data=bounded_html(client,url)
                diagnostics=[]
                rows,rejected=parse_detail(data.decode('utf-8'),code,diagnostics)
                error='RejectedFullROMRecord' if diagnostics else None if rows else 'NoFullROMRecord'
                return url,rows,rejected,hashlib.sha256(data).hexdigest(),error,200
            except Exception as exc:
                if getattr(exc,'fmr_batch_deadline',False):raise
                status=getattr(getattr(exc,'response',None),'status_code',None)
                return url,[],0,None,type(exc).__name__,status
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            results=map(fetch,todo) if max_workers==1 else pool.map(fetch,todo)
            for url,rows,rejected,digest,error,status in results:
                # An unavailable refresh is not permission to erase prior source evidence.
                retained=rows or saved.get(url,{}).get('rows',[])
                delay=86400 if status in (404,410) or error in ('NoFullROMRecord','RejectedFullROMRecord') else 900
                entry={'revision':HISTORY_REVISION,'url':url,'sha256':digest,'records':len(retained),
                       'rows':retained,'checked_at':now,'rejected':rejected,'error':error,'status':status,
                       'next_check':now+delay if error else now+DETAIL_TTL}
                if checkpoint:checkpoint.save(code,url,entry)
                saved[url]=entry
                manifest['detail_pages'].append({k:entry[k] for k in ('url','sha256','records','checked_at','rejected')})
                if error:
                    manifest['errors'].append({'stage':'detail','url':url,'error':error,'status':status})
                    retry_at.append(entry['next_check'])
                records.extend(retained)
        manifest['pending_pages']=len(pending)
        manifest['pending_priority']=[u for u in pending if u in repair]
        manifest['retry_after']=min(retry_at) if retry_at else None
        manifest['rejected_records']=sum(p.get('rejected') or 0 for p in manifest['detail_pages'])
        # Keep evidence for temporarily removed archive links, without claiming coverage.
        if checkpoint:
            manifest['revision']=HISTORY_REVISION
        unique = {};blocked_keys=set()
        for row in sorted(records, key=lambda r: ('.debug' in r['link'], not bool(r.get('md5')), r['link'])):
            n = normalize(row, 'xiaomi')
            key = tuple(n[k] for k in ('device_codename','region_code','version_str','package_type'))
            if key in unique and (unique[key]['link'] != row['link'] or (unique[key].get('md5') and row.get('md5') and unique[key]['md5']!=row['md5'])):
                # The frozen schema admits one package per natural key. Preserve
                # every alternate in the private manifest; never claim it was published.
                kind,blocking=conflict_kind(unique[key],row)
                if blocking:blocked_keys.add(key)
                manifest['conflicts'].append({'key':list(key),'kind':kind,'blocking':blocking,'selected':{k:unique[key].get(k) for k in ('link','md5')},'alternate':row})
            elif key not in unique:
                unique[key] = row
        if checkpoint and blocked_keys:
            for url in versions:
                entry=saved.get(url,{})
                review=any(tuple(normalize(row,'xiaomi')[k] for k in ('device_codename','region_code','version_str','package_type')) in blocked_keys for row in entry.get('rows',[]))
                if review and not entry.get('review_needed'):
                    entry['review_needed']=True;checkpoint.save(code,url,entry)
        if not unique and not pending:
            manifest['errors'].append({'stage': 'coverage', 'error': 'NoHistoryFound'})
        return sorted((row for key,row in unique.items() if key not in blocked_keys), key=lambda r: (r['date'], r['version'], r['link']), reverse=True), manifest
    finally:
        if owned:
            client.close()
