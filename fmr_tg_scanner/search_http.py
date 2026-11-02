"""Bounded, cached public metadata transport shared by every search task."""
import hashlib
import json
import os
import re
import time
from urllib.parse import parse_qs,urlsplit,urljoin
import zlib
import httpx


class WorkBudgetReached(RuntimeError):pass
class SearchTransportError(RuntimeError):pass


def metadata_url(url):
    p=urlsplit(url)
    if p.scheme!='https' or p.username or p.password or p.port not in (None,443) or p.fragment:
        raise ValueError('Only HTTPS metadata origins are allowed')
    q=parse_qs(p.query,keep_blank_values=True)
    if any(len(v)!=1 for v in q.values()):raise ValueError('Duplicate query parameter')
    if p.hostname=='t.me':
        preview=re.fullmatch(r'/s/[a-z][a-z0-9_]{3,31}',p.path)
        embed=re.fullmatch(r'/[a-z][a-z0-9_]{3,31}/[1-9][0-9]{0,14}',p.path)
        profile=re.fullmatch(r'/[a-z][a-z0-9_]{3,31}',p.path)
        if preview:
            if set(q)-{'q','before'}:raise ValueError('Unknown preview query')
            if 'before' in q and not re.fullmatch('[1-9][0-9]{0,14}',q['before'][0]):raise ValueError('Invalid floor')
            if 'q' in q and not 1<=len(q['q'][0])<=128:raise ValueError('Invalid search query')
        elif embed:
            if not q or set(q)-{'embed','mode'} or q.get('embed')!=['1'] or q.get('mode',['tme'])!=['tme']:
                raise ValueError('Only a message embed is allowed')
        elif profile and not q:pass
        else:raise ValueError('Only public TG profiles, previews and exact message embeds are allowed')
    elif p.hostname in ('lyzem.com','www.lyzem.com','chatcrawler.link'):
        if not (p.path=='/' or re.fullmatch(r'/(?:[a-z]{2}/)?search(?:/messages)?/?',p.path)):
            raise ValueError('Only public index search forms are allowed')
        if set(q)-{'q','query','search','page','type','lang'}:raise ValueError('Unsupported index query')
    elif p.hostname=='raw.githubusercontent.com':
        if p.path!='/XiaomiFirmwareUpdater/miui-updates-tracker/master/data/devices.yml' or q:
            raise ValueError('Only the public device dictionary is allowed')
    elif p.hostname=='api.github.com':
        if not (p.path=='/search/repositories' or re.fullmatch(r'/repos/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/(?:readme|releases)',p.path) or re.fullmatch(r'/users/[A-Za-z0-9_-]{1,39}',p.path)):
            raise ValueError('Only public project metadata is allowed')
    else:raise ValueError('Not an enabled metadata origin')
    return url


class SearchHTTP:
    def __init__(self,db,emit,*,max_requests=180,max_seconds=480,client=None,clock=time.monotonic):
        self.db,self.emit,self.clock=db,emit,clock
        self.limit=max_requests;self.deadline=clock()+max_seconds
        self.requests=0;self.cache_hits=0;self.bytes=0;self.errors=0;self.last_request={}
        self.owned=client is None
        self.client=client or httpx.Client(timeout=20,follow_redirects=False,headers={
            'User-Agent':'Mozilla/5.0 (compatible; FindMyROM metadata research)',
            'Accept':'text/html,application/json,text/plain'})
        db.execute('CREATE TABLE IF NOT EXISTS tg_http_cache(url TEXT PRIMARY KEY,expires REAL,body BLOB)')
        db.execute('DELETE FROM tg_http_cache WHERE expires<?',(time.time(),))
    def get(self,url,*,ttl=3600,headers=None):
        metadata_url(url)
        old=self.db.query('SELECT body FROM tg_http_cache WHERE url=? AND expires>?',(url,time.time()))
        if old:
            self.cache_hits+=1
            return zlib.decompress(old[0]['body']).decode()
        host=urlsplit(url).hostname
        for attempt in range(3):
            if self.requests>=self.limit or self.clock()>=self.deadline:raise WorkBudgetReached('Batch request/time budget reached')
            # One shared host rate limiter, rather than multiplying rate by target.
            wait=max(0,self.last_request.get(host,0)+1.1-self.clock())
            if os.environ.get('FMR_TEST_NO_SLEEP')!='1' and wait:time.sleep(wait)
            self.requests+=1;self.last_request[host]=self.clock();started=self.clock()
            try:
                with self.client.stream('GET',url,headers=headers,timeout=min(20,max(.1,self.deadline-self.clock()))) as response:
                    self.emit({'event':'search_http','host':host,'url':url,'status':response.status_code,'attempt':attempt+1})
                    if response.status_code in (301,302,303,307,308) and host=='t.me' and re.fullmatch(r'/s/[a-z][a-z0-9_]{3,31}',urlsplit(url).path):
                        destination=urljoin(url,response.headers.get('Location',''))
                        expected='https://t.me/'+urlsplit(url).path.rsplit('/',1)[-1]
                        if destination.rstrip('/')!=expected:raise SearchTransportError('Preview redirected away from the same public profile')
                        return self.get(expected,ttl=ttl)
                    if response.status_code in (429,500,502,503,504):
                        self.errors+=1
                        if attempt==2:raise SearchTransportError('HTTP '+str(response.status_code))
                        try:delay=min(30,max(1,float(response.headers.get('Retry-After',2**attempt))))
                        except ValueError:delay=2**attempt
                        if os.environ.get('FMR_TEST_NO_SLEEP')!='1':time.sleep(delay)
                        continue
                    if response.status_code!=200:
                        self.errors+=1
                        raise SearchTransportError('HTTP '+str(response.status_code))
                    media=response.headers.get('Content-Type','').split(';')[0].lower()
                    if media not in ('text/html','text/plain','application/json','application/xhtml+xml'):
                        raise SearchTransportError('Non-metadata response')
                    body=bytearray()
                    for chunk in response.iter_bytes(32768):
                        self.bytes+=len(chunk);body.extend(chunk)
                        if len(body)>2*1024*1024:raise SearchTransportError('Metadata body limit')
                    text=body.decode('utf-8',errors='replace')
                    compressed=zlib.compress(text.encode())
                    if len(compressed)<=256*1024:
                        self.db.execute('INSERT INTO tg_http_cache VALUES (?,?,?) ON CONFLICT(url) DO UPDATE SET expires=excluded.expires,body=excluded.body',(url,time.time()+ttl,compressed))
                    if self.requests%20==0:
                        total=self.db.query('SELECT COALESCE(SUM(length(body)),0) AS n FROM tg_http_cache')[0]['n']
                        if total>32*1024*1024:self.db.execute('DELETE FROM tg_http_cache WHERE url IN (SELECT url FROM tg_http_cache ORDER BY expires LIMIT 100)')
                    self.emit({'event':'search_response','url':url,'bytes':len(body),'sha256':hashlib.sha256(body).hexdigest(),'elapsed_ms':round((self.clock()-started)*1000)})
                    return text
            except (httpx.TimeoutException,httpx.TransportError) as exc:
                self.errors+=1
                if attempt==2:raise SearchTransportError(type(exc).__name__) from None
        raise SearchTransportError('Unavailable metadata')
    def close(self):
        if self.owned:self.client.close()
