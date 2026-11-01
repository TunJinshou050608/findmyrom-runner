"""Real catalogue adapters; provenance and blockers are first-class records.

Metadata HTTP deliberately refuses firmware bodies. A manufacturer registry
entry is never treated as successful device/release discovery.
"""
import hashlib
import re
import time
from urllib.parse import urljoin, urlsplit, unquote, quote, urlunsplit
import xml.etree.ElementTree as ET

from bs4 import BeautifulSoup
import httpx
import yaml

ATLAS = 'https://fwatlas.com'
ATLAS_BRANDS = ('xiaomi','google','samsung','motorola','oneplus','oppo','realme','vivo','asus','nothing')
DEFERRED = {'sony':'Awaiting user-supplied Xperia adapter'}
RESTRICTED = {'huawei':'No public full-stock firmware catalogue configured',
              'honor':'No public full-stock firmware catalogue configured'}
WEB_BRANDS = ('xiaomi','google','oneplus','samsung','motorola')
OEM_HOSTS = {
    'sony': ('sonymobile.com','sony.net'),
    'xiaomi': ('d.miui.com','xiaomi.com'),
    'google': ('dl.google.com','dl.googleusercontent.com'),
    'samsung': ('samsungmobile.com','samsung.com','ospserver.net','sslcs.cdngc.net'),
    'oneplus': ('allawnofs.com','allawnfs.com','oneplus.net','oneplus.com','oneplusmobile.com','oplusmobile.com','oppomobile.com','coloros.com','heytapmobile.com'),
    'oppo': ('allawnofs.com','allawnfs.com','oplusmobile.com','oppomobile.com','coloros.com','heytapmobile.com','oppo.com'),
    'realme': ('allawnofs.com','allawnfs.com','realme.com','realme.net','oplusmobile.com','oppomobile.com','coloros.com','heytapmobile.com'),
    'vivo': ('vivo.com','vivo.com.cn','vivoglobal.com','bbk.com'),
    'asus': ('asus.com','asus.com.cn'),
    'nothing': ('android.googleapis.com','nothing.tech','nothing.community','nothingos.com'),
    'motorola': ('motorola.com','lenovo.com','motorola.com.cn'),
}
MIRROR_HOSTS = {'motorola': ('mirrors.lolinet.com',)}
METADATA_HOSTS = {'raw.githubusercontent.com','fwatlas.com','developers.google.com','xmfirmwareupdater.com',
                  'fota-cloud-dn.ospserver.net','doc.samsungmobile.com',
                  'consumer.huawei.com','www.honor.com','igoreisberg.com','app.swup.update.sony.net'}


def text(node):
    return node.get_text(' ', strip=True) if node else ''


def safe_url(url):
    p = urlsplit(url)
    if p.scheme != 'https' or not p.hostname or p.username or p.password or p.port or any(ord(c)<33 for c in url):
        exc=ValueError('Unsafe catalogue URL')
        exc.catalogue_url_issues=[name for name,condition in (
            ('scheme',p.scheme!='https'),('host',not p.hostname),('credentials',bool(p.username or p.password)),
            ('port',bool(p.port)),('whitespace',any(ord(c)<33 for c in url))) if condition]
        raise exc
    return p


def download_origin(vendor, url):
    p = safe_url(url)
    for root in OEM_HOSTS.get(vendor, ()):
        if p.hostname == root or p.hostname.endswith('.'+root):
            return 'oem'
    for root in MIRROR_HOSTS.get(vendor, ()):
        if p.hostname == root:
            return 'archive_mirror'
    return None


def normalize_download(vendor, url):
    # Some OEM support archives still redirect to HTTP or an unescaped filename.
    # Upgrade only a known OEM host; never weaken metadata/redirect allowlists.
    p = urlsplit(url)
    if (p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password
            or p.port not in (None,80 if p.scheme=='http' else 443) or any(ord(c)<32 or ord(c)==127 for c in url)):
        return url
    if any(p.hostname == root or p.hostname.endswith('.'+root) for root in OEM_HOSTS.get(vendor, ())):
        return urlunsplit(('https', p.hostname, quote(p.path, safe='/%:@!$&\'()*+,;=-._~'),
            quote(p.query,safe='/%?:@!$&\'()*+,;=-._~'),p.fragment))
    return url


def canonical_code(vendor, upstream):
    value = re.sub('[^a-z0-9-]+','-',upstream.lower()).strip('-')
    if not value:
        raise ValueError('Missing device identity')
    result = value if vendor == 'xiaomi' else vendor+'-'+value
    if len(result)>32:
        result=result[:21].rstrip('-')+'-'+hashlib.sha256((vendor+':'+upstream).encode()).hexdigest()[:10]
    return result


class MetadataHTTP:
    def __init__(self, client=None):
        self.client=client or httpx.Client(timeout=30,follow_redirects=False,
            headers={'User-Agent':'FindMyROM-Catalogue/1.0','Accept':'text/html,application/json,application/xml,text/plain'})
        self.owned=client is None
        self.cache={}
        self.snapshots={}

    def close(self):
        if self.owned:self.client.close()

    def get(self,url,cookies=None):
        for attempt in range(3):
            try:
                return self._get(url,cookies)
            except (httpx.TimeoutException,httpx.NetworkError):
                if attempt==2:raise
            except httpx.HTTPStatusError as exc:
                if attempt==2 or exc.response.status_code not in (429,500,502,503,504):raise
            time.sleep(0.2*(2**attempt))

    def _get(self,url,cookies=None):
        cache_key=(url,tuple(sorted((cookies or {}).items())))
        if cache_key in self.cache:return self.cache[cache_key]
        for _ in range(5):
            p=safe_url(url)
            if p.hostname not in METADATA_HOSTS or re.search(r'\.(zip|ozip|tgz|gz|img|enc[24]|lz4|bin)(?:$|/)',unquote(p.path),re.I):
                raise ValueError('Metadata adapter refused a firmware request')
            with self.client.stream('GET',url,cookies=cookies) as r:
                if r.status_code in (301,302,303,307,308):
                    url=urljoin(url,r.headers.get('Location',''))
                    continue
                r.raise_for_status()
                media=r.headers.get('Content-Type','').lower().split(';')[0]
                if media not in ('text/html','text/plain','application/json','application/xml','text/xml','application/xhtml+xml'):
                    raise ValueError('Metadata adapter refused binary content')
                data=bytearray()
                for chunk in r.iter_bytes(65536):
                    data.extend(chunk)
                    if len(data)>8*1024*1024:raise ValueError('Metadata response too large')
            result=bytes(data)
            self.cache[cache_key]=result
            self.snapshots[url]=hashlib.sha256(result).hexdigest()
            return result
        raise ValueError('Too many metadata redirects')


def _same_atlas(url):
    p=safe_url(url)
    return p.hostname=='fwatlas.com'


def atlas_devices(vendor, http):
    if vendor not in ATLAS_BRANDS:raise ValueError('Unsupported catalogue vendor')
    todo=[ATLAS+'/'+vendor]
    seen=set(); devices={}
    while todo:
        url=todo.pop(0)
        if url in seen:continue
        if len(seen)>=200:raise ValueError('Catalogue pagination bound exceeded')
        seen.add(url)
        soup=BeautifulSoup(http.get(url),'html.parser')
        for a in soup.select('a[href]'):
            absolute=urljoin(url,a['href']);p=urlsplit(absolute)
            if p.hostname!='fwatlas.com':continue
            segments=p.path.strip('/').split('/')
            if len(segments)==2 and segments[0]==vendor and re.fullmatch('[a-z0-9-]+',segments[1]):
                slug=segments[1]
                name=text(a.find(['h2','h3','h4'])) or text(a)
                name=re.sub(r'\s+(?:\d+\s+(?:firmware|versions?|builds?)|View\b).*','',name,flags=re.I)
                devices.setdefault(slug,{'vendor':vendor,'upstream_id':slug,'name':name[:128] or slug,
                    'page':ATLAS+'/'+vendor+'/'+slug,'source_kind':'community_index'})
            if p.path.rstrip('/')=='/'+vendor and p.query and ('page=' in p.query or 'cursor=' in p.query):todo.append(absolute)
    if not devices:raise ValueError('No devices discovered; not a supported-brand success')
    return sorted(devices.values(),key=lambda d:d['upstream_id'])


def xiaomi_devices(http):
    source='https://raw.githubusercontent.com/XiaomiFirmwareUpdater/miui-updates-tracker/master/data/devices.yml'
    rows=yaml.safe_load(http.get(source))
    if not isinstance(rows,dict):raise ValueError('Invalid upstream Xiaomi device dictionary')
    devices={}
    for variant,labels in rows.items():
        code=str(variant).lower().split('_',1)[0]
        if not re.fullmatch('[a-z0-9-]{2,32}',code):continue
        name=labels[0] if isinstance(labels,list) and labels else code
        route=str(variant).split('_',1)[0]
        devices.setdefault(code,{'vendor':'xiaomi','upstream_id':code,'name':str(name)[:128],
            'codename':code,'route_code':route,'page':'https://xmfirmwareupdater.com/archive/miui/'+route+'/',
            'source_kind':'community_index','catalogue_source':source})
    if not devices:raise ValueError('No Xiaomi device catalogue discovered')
    return sorted(devices.values(),key=lambda d:d['upstream_id']),[]


def google_catalogue(http):
    devices={};all_releases={};errors=[]
    for role,page,ack in [('full_ota','ota','nexus-ota-tos'),('factory','images','nexus-image-tos')]:
        try:
            soup=BeautifulSoup(http.get('https://developers.google.com/android/'+page+'?hl=en',
                                       cookies={'devsite_wall_acks':ack}),'html.parser')
            found=0
            for table in soup.select('table'):
                heading=table.find_previous(['h2','h3'])
                code=heading.get('id','') if heading else ''
                if not re.fullmatch('[a-z0-9-]{2,32}',code) or code in ('legal','instructions'):continue
                for tr in table.select('tr'):
                    a=tr.find('a',href=re.compile(r'https://(?:dl\.google\.com|dl\.googleusercontent\.com)/'))
                    if not a or not urlsplit(a['href']).path.endswith('.zip'):continue
                    cells=tr.find_all(['td','th'])
                    digest=next((text(n) for n in cells if re.fullmatch('[0-9a-fA-F]{64}',text(n))),None)
                    if not digest:
                        match=re.search(r'\b[0-9a-fA-F]{64}\b',text(tr));digest=match[0] if match else None
                    if not digest:continue
                    version=text(cells[0]);found+=1
                    devices.setdefault(code,{'vendor':'google','upstream_id':code,'codename':code,
                        'name':text(heading),'page':'https://developers.google.com/android/'+page+'?hl=en','source_kind':'oem'})
                    all_releases.setdefault(code,[]).append({'vendor':'google','device_id':code,'version':version,
                        'region':'Global','android_version':version.split(',')[0],'kind':role,'download_url':a['href'],
                        'sha256':digest.lower(),'origin_kind':'oem','metadata_source':'https://developers.google.com/android/'+page})
            if not found:errors.append({'source':page,'error':'NoFirmwareTables'})
        except Exception as exc:
            if getattr(exc,'fmr_batch_deadline',False):raise
            errors.append({'source':page,'error':type(exc).__name__})
    if not devices:raise ValueError('Google catalogue unavailable')
    for code,device in devices.items():
        majors=[int(m[1]) for row in all_releases[code] if (m:=re.match(r'^(\d+)',row['android_version']))]
        device['android_release']=min(majors,default=13)
    return sorted(devices.values(),key=lambda d:d['upstream_id']),all_releases,errors


def atlas_releases(device,http):
    vendor=device['vendor'];base=device['page']
    soup=BeautifulSoup(http.get(base),'html.parser')
    facts={text(dt).lower():text(dt.find_next_sibling('dd')) for dt in soup.select('dl dt')}
    code=facts.get('codename') or device.get('codename') or device['upstream_id']
    if vendor=='samsung':
        match=re.search(r'\bSM-[A-Z0-9]+\b',facts.get('model numbers','')+' '+text(soup.find('h1')))
        if match:code=match[0]
    device={**device,'codename':code,'canonical_code':canonical_code(vendor,code),
            'models':facts.get('model numbers','')}
    rows={};pages=[(base,soup)];seen=set()
    while pages:
        url,soup=pages.pop(0)
        if url in seen:continue
        if len(seen)>=200:raise ValueError('Release pagination bound exceeded')
        seen.add(url)
        if soup is None:soup=BeautifulSoup(http.get(url),'html.parser')
        for tr in soup.select('table tbody tr'):
            a=tr.select_one('a[href]')
            if not a:continue
            detail=urljoin(base,a['href'])
            if not _same_atlas(detail) or not urlsplit(detail).path.startswith('/'+vendor+'/'+device['upstream_id']+'/'):continue
            cells={n.get('data-label','').lower():text(n) for n in tr.select('td[data-label]')}
            version=text(a.find('code')) or text(a)
            region=tr.get('data-region') or cells.get('region') or (text(tr.find('th').find('code')) if tr.find('th') and tr.find('th').find('code') else 'Global')
            timestamp=tr.find('time')
            row={'vendor':vendor,'device_id':device['upstream_id'],'version':version,'region':region,
                 'kind':tr.get('data-type') or cells.get('type') or 'samsung_firmware',
                 'android_version':cells.get('os','unknown'),'date':timestamp.get('datetime') if timestamp else None,
                 'size':cells.get('size'),'detail_page':detail,'origin_kind':'unresolved',
                 'metadata_source':base}
            rows[(version,region,row['kind'],detail)]=row
        for a in soup.select('a[href]'):
            next_url=urljoin(url,a['href']);p=urlsplit(next_url)
            if p.hostname=='fwatlas.com' and p.path.rstrip('/')==urlsplit(base).path.rstrip('/') and ('page=' in p.query or 'cursor=' in p.query):
                pages.append((next_url,None))
    return device,list(rows.values())


def resolve_atlas(release,http):
    from .download_refresh import expiring, renewable, VENDORS
    result=dict(release)
    soup=BeautifulSoup(http.get(release['detail_page']),'html.parser')
    facts={text(dt).lower():text(dt.find_next_sibling('dd')) for dt in soup.select('dl dt')}
    for k in ('md5','sha-256'):
        value=facts.get(k,'').split()[0] if facts.get(k) else ''
        if re.fullmatch('[0-9a-fA-F]{32}|[0-9a-fA-F]{64}',value):result['md5' if k=='md5' else 'sha256']=value.lower()
    result['filename']=facts.get('filename') or facts.get('file name')
    expected=str(release['version']).split('/')[0]
    identity=facts.get('version','')+' '+text(soup.find('h1'))
    clean=lambda value: re.sub('[^a-z0-9]','',value.lower())
    if not clean(expected) or clean(expected) not in clean(identity):
        raise ValueError('Version detail redirected or mismatched the requested build')
    candidates=[urljoin(release['detail_page'],a['href']) for a in soup.select('a[href]')]
    landing=next((u for u in candidates if urlsplit(u).hostname=='fwatlas.com' and urlsplit(u).path.startswith('/download/')),None)
    if landing:
        # Follow only the index's public download-page link. Capture an OEM
        # redirect's Location without requesting any firmware response body.
        url=landing
        for _ in range(5):
            p=safe_url(url)
            if p.hostname!='fwatlas.com':break
            with http.client.stream('GET',url,headers={'Referer':landing}) as response:
                if response.status_code in (301,302,303,307,308):
                    url=normalize_download(release['vendor'],urljoin(url,response.headers.get('Location','')))
                    target=urlsplit(url)
                    if target.hostname!='fwatlas.com':
                        result['resolver_observation']={'host':target.hostname,
                            'suffix':target.path.rsplit('.',1)[-1].lower()[-12:] if '.' in target.path.rsplit('/',1)[-1] else 'none'}
                    if download_origin(release['vendor'],url):
                        candidates.append(url)
                        break
                    continue
                response.raise_for_status()
                if not response.headers.get('Content-Type','').lower().startswith('text/html'):
                    raise ValueError('Download resolver refused a firmware body')
                data=bytearray()
                for chunk in response.iter_bytes(65536):
                    data.extend(chunk)
                    if len(data)>2*1024*1024:raise ValueError('Download landing too large')
            page=BeautifulSoup(bytes(data),'html.parser')
            candidates.extend(urljoin(url,a['href']) for a in page.select('a[href]'))
            start=next((a for a in page.select('a[href]') if 'start download' in text(a).lower()),None)
            if not start:break
            next_url=normalize_download(release['vendor'],urljoin(url,start['href']))
            if next_url==url:break
            if download_origin(release['vendor'],next_url):
                candidates.append(next_url)
                break
            url=next_url
    for url in candidates:
        url=normalize_download(release['vendor'],url)
        try:origin=download_origin(release['vendor'],url)
        except ValueError:continue
        if origin and re.search(r'\.(zip|ozip|tgz|tar\.gz|enc[24])$',unquote(urlsplit(url).path),re.I):
            result.update(download_url=url,origin_kind=origin,resolution='resolved')
            if expiring(url) and release['vendor'] in VENDORS and landing:
                return renewable(result,landing)
            return result
    result.update(resolution='interactive_or_unresolved_download',landing_page=landing)
    return result


def samsung_history(model,region,http):
    if not re.fullmatch('SM-[A-Z0-9]+',model) or not re.fullmatch('[A-Z0-9]{3}',region):
        raise ValueError('Invalid Samsung model/CSC')
    data=http.get('https://fota-cloud-dn.ospserver.net/firmware/'+region+'/'+model+'/version.xml')
    if b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():raise ValueError('Unsafe XML')
    root=ET.fromstring(data)
    builds=[]
    for node in root.iter():
        value=(node.text or '').strip()
        if node.tag.rsplit('}',1)[-1] in ('latest','value') and re.fullmatch('[A-Za-z0-9_/.-]{4,200}',value):
            builds.append(value)
    return sorted(set(builds))


def product_devices(vendor,http):
    pages={'huawei':'https://consumer.huawei.com/en/phones/','honor':'https://www.honor.com/global/phones/'}
    if vendor not in pages:raise ValueError('Unknown product catalogue')
    soup=BeautifulSoup(http.get(pages[vendor]),'html.parser')
    result={}
    for a in soup.select('a[href]'):
        url=urljoin(pages[vendor],a['href']);p=urlsplit(url)
        if p.hostname!=urlsplit(pages[vendor]).hostname:continue
        m=re.fullmatch(r'/(?:en|global)/phones/([a-z0-9-]+)/?',p.path)
        if not m:continue
        label=text(a) or (a.find('img').get('alt') if a.find('img') else '') or m[1]
        result.setdefault(m[1],{'vendor':vendor,'upstream_id':m[1],'name':label[:128],
                               'page':url,'source_kind':'oem_product_catalogue'})
    if not result:raise ValueError('No product devices discovered')
    return sorted(result.values(),key=lambda d:d['upstream_id'])
