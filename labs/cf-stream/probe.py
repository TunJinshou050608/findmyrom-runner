"""Run only on self-hosted runner: no firmware/image files are saved."""
import hashlib
import json
import os
from pathlib import Path
import time
import urllib.request
import urllib.error

BASE='https://findmyrom.com/__fmr-stream-lab/'
EXPECTED={
 'google': {'init_boot':'ba2c8c98c11847f4e60f7fce6d637d3d3fff580015f69ff13df414a22fc76ca6','boot':'d8bea3dd4c39879a6ee0cef0e2b4ce66ac57f253b78a4dfcd325dc3640b131c3'},
 'oplus': {'init_boot':'486cc925a533fec94cccdb5d1650f539d9c45880a0fae415337c964432b8dffb','boot':'d0f4a5119b3859a524be142b42b4cdf9c12a1d06d58ee5bc30dfb68a26fe6d99'},
 'vivo': {'init_boot':'d6e88f5158c98348829e3f31824ce9dfd5c04f8c9e9c4d62ad958a3f83cf2026','boot':'3be2d35fa3cade10077b6a17583a735fcbb832b34ff67b7921a0e992ccde155f'},
 'samsung': {'init_boot':'ef1c6c13fad89b25730b8cfcbab5ad319c53287b6484e8d0dbbec3829f06f0e9','boot':'54688aff1c242f6fdd92f80b8d4b96917d7abcef89c6bc0210ce803a6f780e4d'},
}

def get(path):
    return urllib.request.urlopen(urllib.request.Request(BASE+path,headers={
        'Authorization':'Bearer '+os.environ['CF_STREAM_LAB_TOKEN'],'Accept-Encoding':'identity'}),timeout=180)

def main():
    results=[]
    try:
        with get('capabilities') as r: capabilities=json.loads(r.read(65536))
    except urllib.error.HTTPError as e:
        capabilities={'status':e.code,'error':e.read(8192).decode(errors='replace')}
    print(json.dumps({'capabilities':capabilities}),flush=True)
    if os.environ.get('CF_STREAM_LAB_MODE')=='matrix':
        for brand,targets in EXPECTED.items():
            for target,expected in targets.items():
                result={'brand':brand,'partition':target};start=time.monotonic();size=0;h=hashlib.sha256()
                try:
                    with get(f'download/{brand}/{target}') as r:
                        result['headers']={k:v for k,v in r.headers.items() if k.lower().startswith('x-fmr')}
                        while chunk:=r.read(65536):
                            if not size: result['first_byte_seconds']=round(time.monotonic()-start,3)
                            size+=len(chunk)
                            if size>256*1024*1024: raise ValueError('Output limit exceeded')
                            h.update(chunk)
                    result.update(status='passed' if h.hexdigest()==expected else 'failed',sha256=h.hexdigest())
                except urllib.error.HTTPError as e:
                    result.update(status='failed',http_status=e.code,error=e.read(8192).decode(errors='replace'))
                except Exception as e: result.update(status='failed',error=type(e).__name__+': '+str(e))
                result.update(bytes=size,elapsed_seconds=round(time.monotonic()-start,3))
                print(json.dumps(result),flush=True);results.append(result)
    Path('cf-stream-results.json').write_text(json.dumps({'capabilities':capabilities,'results':results},indent=2)+'\n')
    if any(r['status']=='failed' for r in results) or 'error' in capabilities: raise SystemExit(1)

if __name__=='__main__': main()
