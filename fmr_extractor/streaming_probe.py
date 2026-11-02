"""Opt-in on-demand extraction experiment; no catalogue, D1 or vault writes.

Only the CLI resolves the fixed public test cases. This is deliberately not an
HTTP endpoint accepting user URLs. Consumers pull bounded chunks and may close
the generator; no firmware or image is written to disk.
"""
import argparse
import bz2
import contextlib
import hashlib
import io
import json
import lzma
import re
import resource
import struct
import tarfile
import time
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import lz4.frame
import zstandard as zstd
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .containers import StoredMember
from .payload import fields, one
from .patcher import LIMITS as IMAGE_LIMITS

LIMITS = dict(IMAGE_LIMITS, recovery=(65536, 256*1024*1024, b'ANDROID!'))

CHUNK = 256 * 1024
BUDGET = 250 * 1024 * 1024
ALLOWED = ('dl.google.com', 'allawnfs.com', 'allawnofs.com',
           'vivoglobal.com', 'vivo.com.cn', 'cloud-neofussvr.samsungmobile.com')


class RangeReader(io.RawIOBase):
    def __init__(self, url, client, headers=None, budget=BUDGET, refresh=None, prefetch=False):
        parsed = urlsplit(url)
        host = (parsed.hostname or '').lower()
        if (not any(host == h or host.endswith('.'+h) for h in ALLOWED)
                or parsed.username or parsed.password or parsed.port not in (None, 80, 443)
                or parsed.scheme not in ('https', 'http')
                or (parsed.scheme == 'http' and host != ALLOWED[-1])):
            raise ValueError('Test source outside fixed official CDN allowlist')
        self.url, self.client, self.headers = url, client, headers or {}
        self.refresh = refresh
        self.prefetch,self.cache_start,self.cache=prefetch,0,b''
        self.budget, self.used, self.requests, self.position = budget, 0, 0, 0
        self.size, self.etag = None, None
        self.deadline = time.monotonic() + 480
        self.encodings = set()
        self.fetch(0, 1)

    def fetch(self, start, length, _renewed=False):
        if (length <= 0 or start < 0 or length > self.budget-self.used
                or time.monotonic() > self.deadline):
            raise ValueError('Range budget, offset or deadline exceeded')
        end = start + length - 1
        headers = dict(self.headers, Range=f'bytes={start}-{end}', **{'Accept-Encoding': 'identity'})
        if self.etag:
            headers['If-Match'] = self.etag
        self.requests += 1
        with self.client.stream('GET', self.url, headers=headers, follow_redirects=False, timeout=30) as r:
            if r.status_code==401 and self.refresh and not _renewed:
                r.close()
                self.headers=self.refresh()
                return self.fetch(start,length,_renewed=True)
            # Close an ignored Range BEFORE reading its multi-GB response body.
            if r.status_code != 206:
                raise ValueError(f'Range HTTP {r.status_code}; body not consumed')
            m = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', r.headers.get('content-range', ''))
            if not m or tuple(map(int, m.groups()[:2])) != (start, end):
                raise ValueError('Invalid exact Content-Range')
            total = int(m[3])
            if end >= total or (self.size is not None and total != self.size):
                raise ValueError('Source size changed')
            etag = r.headers.get('etag')
            if self.etag and etag and etag != self.etag:
                raise ValueError('Source ETag changed')
            if etag and not etag.startswith('W/'):
                self.etag = etag
            self.size = total
            encoding = r.headers.get('content-encoding', 'identity').lower()
            self.encodings.add(encoding)
            host = urlsplit(self.url).hostname
            oplus = any(host == d or host.endswith('.'+d) for d in ('allawnfs.com','allawnofs.com'))
            if encoding != 'identity' and not (encoding == 'utf-8' and oplus):
                raise ValueError('Unsupported encoded Range response')
            result = bytearray()
            for chunk in r.iter_raw(65536):
                self.used += len(chunk)
                if (self.used > self.budget or len(result)+len(chunk) > length
                        or time.monotonic() > self.deadline):
                    raise ValueError('Range response exceeded bounds')
                result.extend(chunk)
            if len(result) != length:
                raise ValueError('Truncated Range response')
            return bytes(result)

    def readable(self): return True
    def seekable(self): return True
    def tell(self): return self.position
    def seek(self, offset, whence=0):
        if whence not in (0, 1, 2): raise ValueError('Invalid whence')
        pos = offset if whence == 0 else self.position+offset if whence == 1 else self.size+offset
        if not 0 <= pos <= self.size: raise ValueError('Seek out of bounds')
        self.position = pos
        return pos
    def read(self, size=-1):
        length = self.size-self.position if size < 0 else min(size, self.size-self.position)
        if length <= 0: return b''
        if self.prefetch and self.cache_start<=self.position and self.position+length<=self.cache_start+len(self.cache):
            data=self.cache[self.position-self.cache_start:self.position-self.cache_start+length]
        else:
            amount=min(self.size-self.position,max(length,1024*1024)) if self.prefetch and length>=4096 else length
            amount=min(amount,self.budget-self.used)
            if amount<length: raise ValueError('Range budget exceeded')
            fetched=self.fetch(self.position,amount)
            if self.prefetch: self.cache_start,self.cache=self.position,fetched
            data=fetched[:length]
        self.position += len(data)
        return data


class DecryptedReader(io.RawIOBase):
    """AES ECB alignment is independent of ZIP's inner compression layer."""
    def __init__(self, parent, key):
        self.parent, self.key, self.position = parent, key, 0
        self.cache_start,self.cache=0,b''
        if parent.size % 16: raise ValueError('Invalid AES container size')
        final = self.decrypt(parent.fetch(parent.size-16, 16))
        padding = final[-1]
        if not 1 <= padding <= 16 or final[-padding:] != bytes([padding])*padding:
            raise ValueError('Invalid Samsung key or PKCS7 padding')
        self.size = parent.size-padding
    def decrypt(self, data):
        decoder = Cipher(algorithms.AES(self.key), modes.ECB()).decryptor()
        return decoder.update(data)+decoder.finalize()
    def readable(self): return True
    def seekable(self): return True
    def tell(self): return self.position
    def seek(self, offset, whence=0):
        if whence not in (0,1,2): raise ValueError('Invalid whence')
        pos = offset if whence == 0 else self.position+offset if whence == 1 else self.size+offset
        if not 0 <= pos <= self.size: raise ValueError('Seek out of bounds')
        self.position=pos
        return pos
    def read(self, size=-1):
        n = self.size-self.position if size < 0 else min(size,self.size-self.position)
        if n <= 0: return b''
        start = self.position//16*16
        stop = (self.position+n+15)//16*16
        if self.cache_start<=self.position and self.position+n<=self.cache_start+len(self.cache):
            result=self.cache[self.position-self.cache_start:self.position-self.cache_start+n]
            self.position+=len(result)
            return result
        # Small ZipExtFile reads must not create hundreds of FUS requests.
        # Keep at most 1MiB plaintext; stop the read-ahead at encrypted EOF.
        stop=min(self.parent.size,max(stop,start+1024*1024))
        data = self.decrypt(self.parent.fetch(start,stop-start))
        self.cache_start,self.cache=start,data
        result = data[self.position-start:self.position-start+n]
        self.position += len(result)
        return result


def bounded_get(client, url, limit=4*1024*1024, **kwargs):
    with client.stream('GET', url, follow_redirects=False, timeout=30, **kwargs) as r:
        r.raise_for_status()
        out=bytearray()
        for chunk in r.iter_bytes(65536):
            out.extend(chunk)
            if len(out)>limit: raise ValueError('Metadata exceeded bound')
        return bytes(out)


def samsung_source(client, model='SM-S928B', region='EUX'):
    """FUS adaptation: topjohnwu/samloader-rs, Apache-2.0; existing license in docs/licenses.

    Kept separate from production's deliberately metadata-only SmartFUS client.
    """
    from fmr_ingest_stock.samsung_fus import AUTH_KEY, HOST, normalize_version
    nonce=auth=''
    def headers():
        return {'User-Agent':'SMART 2.0', 'Content-Type':'application/xml',
                'Authorization':f'FUS nonce="{nonce}", signature="{auth}", nc="", type="", realm="", newauth="1"'}
    def request(endpoint, body=''):
        nonlocal nonce,auth
        with client.stream('POST', HOST+endpoint, headers=headers(),content=body.encode()) as r:
            r.raise_for_status()
            if r.headers.get('nonce'):
                nonce=r.headers['nonce']
                enc=Cipher(algorithms.AES(AUTH_KEY),modes.ECB()).encryptor()
                auth=(enc.update(nonce.encode()[:16].ljust(16,b'0'))+enc.finalize()).hex()
            data=bytearray()
            for chunk in r.iter_bytes(65536):
                data.extend(chunk)
                if len(data)>1024*1024: raise ValueError('FUS metadata exceeded bound')
            if b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper(): raise ValueError('Unsafe XML')
            return bytes(data)
    def logic(source, value):
        return ''.join(source[ord(c)&15] if (ord(c)&15)<len(source) else '.' for c in value)
    def xml(values):
        root=ET.Element('FUSMsg');h=ET.SubElement(root,'FUSHdr')
        for k,v in [('ProtoVer','1.0'),('SessionID','0'),('MsgID','1')]: ET.SubElement(h,k).text=v
        put=ET.SubElement(ET.SubElement(root,'FUSBody'),'Put')
        for k,v in values.items(): ET.SubElement(ET.SubElement(put,k),'Data').text=v
        return root
    version_xml=bounded_get(client,f'https://fota-cloud-dn.ospserver.net/firmware/{region}/{model}/version.xml',headers={'User-Agent':'Kies2.0_FUS'})
    version=normalize_version(ET.fromstring(version_xml).findtext('./firmware/version/latest'))
    request('NF_SmartDownloadGenerateNonce.do')
    root=xml({'ACCESS_MODE':'1','BINARY_NATURE':'1','REQUEST_TYPE':'2','LOGIC_CHECK':logic(version,nonce),
              'BINARY_SW_VERSION':version,'BINARY_LOCAL_CODE':region,'BINARY_MODEL_NAME':model})
    put=root.find('./FUSBody/Put');ET.SubElement(put,'CmdID').text='1'
    get=ET.SubElement(root.find('FUSBody'),'Get');ET.SubElement(get,'CmdID').text='2';ET.SubElement(get,'BINARY_SW_VERSION')
    reply=ET.fromstring(request('NF_SmartDownloadBinaryInform.do',ET.tostring(root,encoding='unicode')))
    status=reply.findtext('./FUSBody/Results/Status')
    if status not in ('200','S00'): raise ValueError('FUS inform status '+str(status))
    data={n.tag:n.findtext('Data') for n in reply.iter() if n.find('Data') is not None}
    filename=data['BINARY_NAME'];path=data['MODEL_PATH']
    if not re.fullmatch(r'[A-Za-z0-9_.-]+\.enc[24]',filename) or not re.fullmatch(r'/[A-Za-z0-9_/-]+/',path):
        raise ValueError('Invalid FUS filename/path')
    version=data.get('BINARY_SW_VERSION',version)
    if filename.endswith('.enc2'):
        seed=f'{region}:{model}:{version}'
    else:
        seed=logic(version,data.get('LOGIC_VALUE_FACTORY') or data['LOGIC_VALUE_HOME'])
    key=hashlib.md5(seed.encode()).digest()
    def init_download():
        root=xml({'BINARY_NAME':filename,'BINARY_SW_VERSION':version,'DEVICE_LOCAL_CODE':region,
                  'DEVICE_MODEL_TYPE':data['DEVICE_MODEL_TYPE'],'LOGIC_CHECK':logic(filename[-25:-9],nonce)})
        request('NF_SmartDownloadBinaryInitForMass.do',ET.tostring(root,encoding='unicode'))
    def refresh():
        request('NF_SmartDownloadGenerateNonce.do')
        init_download()
        return headers()
    init_download()
    url=f'http://cloud-neofussvr.samsungmobile.com/NF_SmartDownloadBinaryForMass.do?file={path}{filename}'
    return url,headers(),key,{'model':model,'region':region,'version':version,'filename':filename},refresh


def payload_stream(remote, base, payload_size, target, metrics):
    def read(offset,length):
        if offset<0 or length<0 or offset+length>payload_size: raise ValueError('Payload read outside container')
        remote.seek(base+offset)
        return remote.read(length)
    magic,version,ms,ss=struct.unpack('>4sQQI',read(0,24))
    if magic!=b'CrAU' or version!=2 or ms>16*1024*1024: raise ValueError('Unsupported payload header')
    manifest=fields(read(24,ms));block=one(manifest,3,4096)
    if block!=4096: raise ValueError('Unsupported payload block size')
    parts=[fields(p) for p in manifest.get(13,[]) if one(fields(p),1)==target.encode()]
    if len(parts)!=1: raise ValueError('Target partition missing or duplicated')
    part=parts[0];info=fields(one(part,7,b''));size=one(info,1,0);expected=one(info,2)
    if not LIMITS[target][0]<=size<=LIMITS[target][1] or not isinstance(expected,bytes) or len(expected)!=32:
        raise ValueError('Invalid payload size/hash')
    plan=[]
    for raw in part.get(8,[]):
        op=fields(raw);kind=one(op,1)
        if kind not in (0,1,6,8,14): raise ValueError(f'Needs source image: operation {kind}')
        ranges=[]
        for rawext in op.get(6,[]):
            ext=fields(rawext);start=one(ext,1,-1)*block;length=one(ext,2,0)*block
            if start<0 or length<=0 or start+length>size: raise ValueError('Invalid extent')
            ranges.append((start,length))
        if not ranges: raise ValueError('Operation without extents')
        cursor=ranges[0][0]
        for start,length in ranges:
            if start!=cursor: raise ValueError('Noncontiguous operation needs target-only staging')
            cursor+=length
        plan.append((ranges[0][0],cursor-ranges[0][0],kind,op))
    plan.sort(key=lambda item:item[0]);cursor=0
    for start,length,_,_ in plan:
        if start!=cursor: raise ValueError('Overlapping/incomplete extents')
        cursor+=length
    if cursor!=size: raise ValueError('Incomplete partition')
    metrics.update(format='payload',output_size=size,operations=len(plan),
                   operation_types=sorted(set(p[2] for p in plan)),integrity='manifest partition SHA256 + operation SHA256')
    digest=hashlib.sha256()
    for _,length,kind,op in plan:
        if kind==6:
            source=io.BytesIO(b'')
        else:
            offset=one(op,2,0);amount=one(op,3,0)
            # Bound compressed operation buffering separately from output size.
            if not 0<amount<=32*1024*1024: raise ValueError('Operation input exceeds 32MiB buffer cap')
            compressed=read(24+ms+ss+offset,amount)
            if hashlib.sha256(compressed).digest()!=one(op,8): raise ValueError('Operation SHA256 mismatch')
            if kind==14:
                params=zstd.get_frame_parameters(compressed)
                if params.window_size>64*1024*1024: raise ValueError('ZSTD window exceeds bound')
                # The deployed C backend's streaming API enforces this value in
                # bytes despite the published Python docs saying KiB. Always
                # check the frame's actual window above as an independent cap.
                source=zstd.ZstdDecompressor(max_window_size=64*1024*1024).stream_reader(io.BytesIO(compressed))
            elif kind==0: source=io.BytesIO(compressed)
            elif kind==1: source=bz2.BZ2File(io.BytesIO(compressed))
            else: source=lzma.LZMAFile(io.BytesIO(compressed))
        written=0
        with source:
            while written<length:
                chunk=(bytes(min(CHUNK,length-written)) if kind==6 else source.read(min(CHUNK,length-written)))
                if not chunk: raise ValueError('Truncated partition operation')
                written+=len(chunk);digest.update(chunk)
                yield chunk
            if kind!=6 and source.read(1): raise ValueError('Operation output exceeds extents')
    if digest.digest()!=expected: raise ValueError('Partition SHA256 mismatch')
    metrics['sha256_verified']=True


def _image_stream(remote, target, metrics):
    """Pull iterator. Closing it closes every nested decoder/container."""
    if target not in ('boot','init_boot','recovery'): raise ValueError('Invalid lab partition')
    with contextlib.ExitStack() as stack:
        archive=stack.enter_context(zipfile.ZipFile(remote))
        nested=[i for i in archive.infolist() if re.fullmatch(r'image-[^/]+\.zip',i.filename.rsplit('/',1)[-1])]
        if nested:
            if len(nested)!=1: raise ValueError('Ambiguous nested ZIP')
            remote=StoredMember(remote,nested[0]);archive=stack.enter_context(zipfile.ZipFile(remote))
            metrics['nested_container_method']='STORED'
        direct=[i for i in archive.infolist() if i.filename.rsplit('/',1)[-1]==target+'.img']
        if direct:
            if len(direct)!=1: raise ValueError('Duplicate image')
            item=direct[0]
            if not LIMITS[target][0]<=item.file_size<=LIMITS[target][1]: raise ValueError('Invalid image size')
            metrics.update(format='nested_zip' if nested else 'zip',output_size=item.file_size,
                           integrity='ZIP member CRC32',compression_method=item.compress_type)
            source=stack.enter_context(archive.open(item))
            while chunk:=source.read(CHUNK): yield chunk
            metrics['crc32_verified']=True
            return
        payload=[i for i in archive.infolist() if i.filename=='payload.bin']
        if payload:
            if len(payload)!=1: raise ValueError('Duplicate payload')
            view=StoredMember(remote,payload[0])
            yield from payload_stream(remote,view.base,view.size,target,metrics)
            return
        # A deflated AP is sequential. Stop as soon as the requested TAR member
        # ends, never drain the rest just to obtain the AP's whole-member CRC.
        aps=[i for i in archive.infolist() if re.fullmatch(r'(?:AP|BL)_[^/]+\.tar(?:\.md5)?',i.filename.rsplit('/',1)[-1])]
        if not aps: raise ValueError('No supported image container')
        for item in sorted(aps,key=lambda i:not i.filename.rsplit('/',1)[-1].startswith('AP_')):
            metrics.update(format='samsung_zip_tar',ap_compression=item.compress_type,
                           integrity='TAR header checksum + image size/magic; whole AP CRC not verified')
            source=stack.enter_context(archive.open(item))
            tar=stack.enter_context(tarfile.open(fileobj=source,mode='r|',bufsize=CHUNK))
            for count,entry in enumerate(tar):
                if count>=4096: raise ValueError('TAR member count exceeded')
                name=entry.name.rsplit('/',1)[-1]
                if name not in (target+'.img',target+'.img.lz4'): continue
                if not entry.isfile() or entry.sparse or entry.size>LIMITS[target][1]: raise ValueError('Invalid TAR target')
                member=stack.enter_context(tar.extractfile(entry))
                decoder=lz4.frame.LZ4FrameDecompressor() if name.endswith('.lz4') else None
                written=0
                while True:
                    chunk=member.read(CHUNK) if not decoder or decoder.needs_input else b''
                    if not chunk and (not decoder or decoder.needs_input): break
                    data=decoder.decompress(chunk,max_length=CHUNK) if decoder else chunk
                    written+=len(data)
                    if written>LIMITS[target][1]: raise ValueError('Image output bound exceeded')
                    if data: yield data
                    if decoder and decoder.eof:
                        if decoder.unused_data or member.read(1): raise ValueError('Trailing LZ4 data')
                        break
                if decoder and not decoder.eof: raise ValueError('Incomplete LZ4 frame')
                if not LIMITS[target][0]<=written<=LIMITS[target][1]: raise ValueError('Invalid image size')
                metrics['output_size']=written
                return
        raise ValueError('Target not found before budget/deadline')


def image_stream(remote,target,metrics):
    """Withhold the final chunk until CRC/hash/decoder validation completes.

    Otherwise a Content-Length client could accept a complete corrupt image
    before the producer's final SHA256 check raises an error.
    """
    stream=_image_stream(remote,target,metrics)
    pending=None;written=0
    try:
        for chunk in stream:
            if pending is None and written==0 and not chunk.startswith(LIMITS[target][2]):
                raise ValueError('Image magic mismatch')
            written+=len(chunk)
            if written>LIMITS[target][1]: raise ValueError('Image output bound exceeded')
            if pending is not None: yield pending
            pending=chunk
        if pending is None or written!=metrics['output_size']:
            raise ValueError('Incomplete image output')
        yield pending
    finally: stream.close()


VIVO='https://asia-sysupwsdl.vivoglobal.com/upgrade/official/officialFiles/PD2357F_EX_A_14.0.16.1.W30.V000L1-update-full_1739959098.zip'
OPLUS='https://gauss-componentotacostmanual-cn.allawnfs.com/remove-f3beae86ff23858de919c5c568891ca7/component-ota/24/10/09/233fa60caa38462e97b29bfa8bc0c0e2.zip'


def http_delivery_probe(remote,target):
    """Real local HTTP delivery on the runner; no public port or resident daemon."""
    from http.server import BaseHTTPRequestHandler, HTTPServer
    import socket
    import threading
    import secrets
    ticket='/'+secrets.token_hex(16)
    states=[]
    class Handler(BaseHTTPRequestHandler):
        protocol_version='HTTP/1.1'
        def log_message(self,*args): pass
        def do_GET(self):
            if self.path!=ticket:
                self.send_error(404)
                return
            state={'sent_bytes':0};states.append(state)
            self.connection.setsockopt(socket.SOL_SOCKET,socket.SO_SNDBUF,65536)
            self.connection.settimeout(20)
            stream=image_stream(remote,target,state)
            try:
                first=next(stream)
                self.send_response(200)
                self.send_header('Content-Type','application/octet-stream')
                self.send_header('Cache-Control','no-store')
                chunked='output_size' not in state
                if not chunked: self.send_header('Content-Length',str(state['output_size']))
                else: self.send_header('Transfer-Encoding','chunked')
                self.end_headers()
                def send(chunk):
                    self.wfile.write(f'{len(chunk):x}\r\n'.encode()+chunk+b'\r\n' if chunked else chunk)
                    state['sent_bytes']+=len(chunk)
                send(first)
                for chunk in stream:
                    send(chunk)
                if chunked: self.wfile.write(b'0\r\n\r\n')
                state['status']='complete'
            except (BrokenPipeError,ConnectionResetError): state['status']='client_disconnected'
            except Exception as e: state.update(status='failed',error=type(e).__name__+': '+str(e))
            finally:
                stream.close()
                state['done']=True
                self.close_connection=True
    server=HTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    url=f'http://127.0.0.1:{server.server_port}{ticket}'
    counter=remote.parent if isinstance(remote,DecryptedReader) else remote
    before=counter.used;t0=time.monotonic();digest=hashlib.sha256();received=0;first=None
    result={}
    try:
        with httpx.Client(trust_env=False,timeout=60) as sink:
            with sink.stream('GET',url) as response:
                response.raise_for_status()
                for chunk in response.iter_raw(65536):
                    if first is None: first=round(time.monotonic()-t0,3)
                    digest.update(chunk);received+=len(chunk)
            result.update(bytes=received,sha256=digest.hexdigest(),first_byte_seconds=first,
                          elapsed_seconds=round(time.monotonic()-t0,3),upstream_bytes=counter.used-before)
            # HTTP reset after first received chunk. Wait for the producer to
            # acknowledge disconnect, allowing a bounded in-flight write.
            with sink.stream('GET',url) as response:
                response.raise_for_status()
                next(response.iter_raw(65536))
                at_cancel=counter.used
            limit=time.monotonic()+35
            while len(states)<2 or not states[-1].get('done'):
                if time.monotonic()>limit: raise TimeoutError('Producer did not stop after HTTP disconnect')
                time.sleep(.02)
            result['cancel']={'producer_status':states[-1]['status'],
                              'extra_upstream_bytes':counter.used-at_cancel,
                              'producer_stopped':True}
        result['status']='passed' if states[0].get('status')=='complete' and received>0 else 'failed'
        result['integrity']=states[0].get('integrity')
    finally:
        server.shutdown();server.server_close();thread.join(timeout=2)
    return result


def run_case(brand, targets, output):
    report={'brand':brand,'runner':__import__('os').environ.get('RUNNER_NAME'),
            'firmware_disk_bytes':0,'image_disk_bytes':0,'results':[]}
    started=time.monotonic();raw=None
    try:
        with httpx.Client(timeout=30,follow_redirects=False) as client:
            if brand=='samsung':
                url,headers,key,identity,refresh=samsung_source(client)
                report['source']=identity
            elif brand=='google':
                page=bounded_get(client,'https://developers.google.com/android/images?hl=en',
                                 cookies={'devsite_wall_acks':'nexus-image-tos'}).decode()
                urls=re.findall(r'https://dl\.google\.com/dl/android/aosp/akita-[^"<>\s]+-factory-[^"<>\s]+\.zip',page)
                if not urls: raise ValueError('No official Pixel 8a factory URL')
                url=urls[0];headers={};key=None
                report['source']={'filename':url.rsplit('/',1)[-1]}
            else: url=VIVO if brand=='vivo' else OPLUS;headers={};key=None
            report['source_host']=urlsplit(url).hostname
            raw=RangeReader(url,client,headers,refresh=refresh if brand=='samsung' else None,prefetch=brand=='google')
            remote=DecryptedReader(raw,key) if key else raw
            report['firmware_size']=raw.size
            for target in targets:
                metrics={'partition':target};t0=time.monotonic();before=raw.used
                digest=hashlib.sha256();size=0;first=True
                try:
                    with contextlib.closing(image_stream(remote,target,metrics)) as stream:
                        for chunk in stream:
                            if first:
                                if chunk[:len(LIMITS[target][2])]!=LIMITS[target][2]: raise ValueError('Image magic mismatch')
                                metrics['first_byte_seconds']=round(time.monotonic()-t0,3)
                                first=False
                            digest.update(chunk);size+=len(chunk)
                    if first or size!=metrics['output_size']: raise ValueError('Incomplete output')
                    metrics.update(status='passed',bytes=size,sha256=digest.hexdigest())
                except Exception as e:
                    metrics.update(status='failed',error=type(e).__name__+': '+str(e))
                metrics.update(elapsed_seconds=round(time.monotonic()-t0,3),upstream_bytes=raw.used-before)
                report['results'].append(metrics)
                print(json.dumps(metrics,ensure_ascii=False),flush=True)
            # Close after first output, then ensure no further upstream reads.
            before=raw.used;stream=image_stream(remote,targets[0],{})
            try:
                next(stream);after=raw.used
                stream.close()
                report['cancel_probe']={'status':'passed' if raw.used==after else 'failed',
                                        'upstream_bytes':after-before,'extra_bytes_after_close':raw.used-after}
            except Exception as e:
                stream.close();report['cancel_probe']={'status':'failed','error':type(e).__name__+': '+str(e)}
            if report['results'][0]['status']=='passed':
                try:
                    reference=next((r for r in report['results'] if brand=='samsung' and r['partition']=='boot' and r['status']=='passed'),report['results'][0])
                    report['http_delivery']=http_delivery_probe(remote,reference['partition'])
                    report['http_delivery']['partition']=reference['partition']
                    if report['http_delivery']['sha256']!=reference['sha256']:
                        raise ValueError('HTTP-delivered image differs from direct extraction')
                except Exception as e:
                    report['http_delivery']={'status':'failed','error':type(e).__name__+': '+str(e)}
    except Exception as e:
        report['error']=type(e).__name__+': '+str(e)
    report.update(elapsed_seconds=round(time.monotonic()-started,3),peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    if raw: report.update(upstream_bytes=raw.used,range_requests=raw.requests,content_encodings=sorted(raw.encodings))
    report['status']='passed' if (report['results'] and all(x['status']=='passed' for x in report['results'])
        and not report.get('error') and report.get('http_delivery',{}).get('status','passed')=='passed') else 'failed'
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(report,ensure_ascii=False),flush=True)
    return report


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--brand',choices=['google','oplus','vivo','samsung'],required=True)
    parser.add_argument('--targets',default='init_boot,boot')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    targets=args.targets.split(',')
    if not targets or not set(targets)<= {'init_boot','boot','recovery'}: parser.error('Invalid targets')
    result=run_case(args.brand,targets,args.output)
    raise SystemExit(0 if result['status']=='passed' else 1)


if __name__=='__main__': main()
