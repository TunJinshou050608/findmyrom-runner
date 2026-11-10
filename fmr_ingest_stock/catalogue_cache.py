"""Bounded task-side text-only cache, with conditional revalidation."""
import base64
import binascii
import hashlib
import json
from contextlib import contextmanager
import re
import time
from urllib.parse import unquote,urlsplit
import zlib
import httpx
from .catalogue_store import digest
from .cache_lifecycle import PageCache,ByteLRU,MEMORY_BYTES,MAX_RETENTION
from .oem_sources import METADATA_HOSTS,safe_url

MEDIA={'text/html','text/plain','application/json','application/xml','text/xml','application/xhtml+xml'}
MAX_BODY=8*1024*1024


class CatalogueDeadline(TimeoutError):
    fmr_batch_deadline=True


class CachedClient:
    def __init__(self,store,client=None,force=False,now=None):
        self.store=store
        self.raw=client or httpx.Client(timeout=30,follow_redirects=False,headers={'User-Agent':'FindMyROM-Catalogue/2.0'})
        self.owned=client is None
        self.force=force
        self.now=now or time.time
        self.stats={'network':0,'cache_hits':0,'not_modified':0}
        self.pages=PageCache(store,now=self.now)
        self.memory=ByteLRU(MEMORY_BYTES,lambda value:len(json.dumps(value,separators=(',',':')).encode()))
        self.deadline=time.monotonic()+45*60

    def close(self):
        self.pages.flush()
        self.stats.update(cache_bytes=self.pages.bytes,cache_entries=len(self.pages.entries),cache_evictions=self.pages.evicted)
        print(json.dumps({'event':'metadata_cache_budget','vendor':self.store.vendor,'bytes':self.pages.bytes,
                          'entries':len(self.pages.entries),'evicted':self.pages.evicted,'memory_bytes':self.memory.used}),flush=True)
        self.memory.clear()
        if self.owned:self.raw.close()

    @staticmethod
    def lifetime(url):
        p=urlsplit(url)
        if p.path.endswith('/downloadCheck'):return 0
        # Public go/download tokens rotate; never persist those landing responses.
        if p.hostname=='fwatlas.com' and p.path.startswith('/download/'):return 0
        if p.hostname=='xmfirmwareupdater.com':return 3600 if p.path.startswith('/archive/') else 30*86400
        if p.hostname=='fwatlas.com':return 3600 if len(p.path.strip('/').split('/'))<=2 else 30*86400
        return 3600

    @staticmethod
    def unpack(row):
        if not isinstance(row,dict) or not isinstance(row.get('checked_at'),(int,float)) or not isinstance(row.get('headers'),dict):
            raise ValueError('Invalid cached metadata fields')
        decoder=zlib.decompressobj()
        body=decoder.decompress(base64.b64decode(row['body'],validate=True),MAX_BODY+1)
        if len(body)>MAX_BODY or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
            raise ValueError('Invalid cached metadata body')
        if hashlib.sha256(body).hexdigest()!=row['digest']:raise ValueError('Cached metadata digest mismatch')
        return body

    @contextmanager
    def stream(self,method,url,**kwargs):
        if time.monotonic()>=self.deadline:raise CatalogueDeadline('Batch time budget reached')
        p=safe_url(str(url))
        if method!='GET' or p.hostname not in METADATA_HOSTS or re.search(r'\.(zip|ozip|tgz|gz|img|enc[24]|lz4|bin)(?:$|/)',unquote(p.path),re.I):
            raise ValueError('Metadata cache refused a binary request')
        ttl=self.lifetime(str(url));now=int(self.now())
        key=self.store.key('page',[str(url),kwargs.get('cookies') or {}])
        cached=self.memory.get(key)
        if cached:self.pages.touch(key)
        if cached is None and ttl:
            cached=self.pages.get(key)
            if cached:self.memory[key]=cached
        cached_body=None
        if cached:
            try:cached_body=self.unpack(cached)
            except (ValueError,TypeError,KeyError,zlib.error,binascii.Error):
                # Rebuild a damaged cache document from its trusted source; never
                # clear the successfully resolved model/release checkpoints.
                cached=None;self.memory.pop(key,None)
        bypass=any(str(k).lower()=='cache-control' and 'no-cache' in str(v).lower() for k,v in (kwargs.get('headers') or {}).items())
        if cached and not self.force and not bypass and now<cached['checked_at']+ttl:
            self.stats['cache_hits']+=1
            response=httpx.Response(200,headers=cached['headers'],content=cached_body,request=httpx.Request('GET',url))
            try:yield response
            finally:response.close()
            return
        headers=dict(kwargs.pop('headers',{}) or {})
        if cached:
            if cached.get('etag'):headers['If-None-Match']=cached['etag']
            if cached.get('last_modified'):headers['If-Modified-Since']=cached['last_modified']
        self.stats['network']+=1
        with self.raw.stream(method,url,headers=headers,**kwargs) as original:
            if original.status_code==304:
                if not cached:raise ValueError('304 without a cached document')
                data=cached_body;cached['checked_at']=now
                cached['retained_until']=now+min(MAX_RETENTION,ttl+86400)
                self.pages.put(key,cached);self.memory[key]=cached
                self.stats['not_modified']+=1
                response=httpx.Response(200,headers=cached['headers'],content=data,request=original.request)
            elif original.status_code==200:
                media=original.headers.get('Content-Type','').lower().split(';')[0]
                if media not in MEDIA and str(url)!='https://igoreisberg.com/xperifirm/resources':raise ValueError('Metadata cache refused binary content')
                data=bytearray()
                for chunk in original.iter_bytes(65536):
                    data.extend(chunk)
                    if len(data)>(2*1024*1024 if str(url)=='https://igoreisberg.com/xperifirm/resources' else MAX_BODY):raise ValueError('Metadata response exceeds bound')
                data=bytes(data)
                response=httpx.Response(200,headers={'Content-Type':media},content=data,request=original.request)
                packed=base64.b64encode(zlib.compress(data)).decode()
                if ttl and len(packed)<1000000:
                    cached={'checked_at':now,'retained_until':now+min(MAX_RETENTION,ttl+86400),'body':packed,'digest':hashlib.sha256(data).hexdigest(),
                        'headers':{'Content-Type':media},'etag':original.headers.get('ETag'),
                        'last_modified':original.headers.get('Last-Modified')}
                    self.pages.put(key,cached);self.memory[key]=cached
            else:
                # Redirect locations and errors are interpreted by the adapters.
                # Their bodies are never read or cached here.
                yield original
                return
        try:yield response
        finally:response.close()
