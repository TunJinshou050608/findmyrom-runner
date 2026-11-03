"""Live Samsung gateway/VPS proof. Consume streams in memory; never save a firmware/image."""
import argparse
import hashlib
import json
import time
import httpx

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--token',required=True)
    parser.add_argument('--partition',default='boot',choices=['boot','init_boot','recovery'])
    args=parser.parse_args()
    site='https://findmyrom.com'
    with httpx.Client(timeout=90,follow_redirects=False,trust_env=False) as client:
        deadline=time.monotonic()+660
        while True:
            caps=client.get('https://extract-origin.findmyrom.com/v1/capabilities')
            caps.raise_for_status()
            info=caps.json()
            print(json.dumps({'event':'samsung_vps_capabilities',**info}),flush=True)
            if info.get('samsung_full_stream'):break
            if time.monotonic()>deadline:raise RuntimeError('VPS automatic update has not applied')
            time.sleep(30)
        response=client.get(site+'/verify/'+args.token+'?partition='+args.partition)
        response.raise_for_status()
        response=client.post(site+'/api/extract-ticket',headers={'Origin':site,'Accept':'application/json'},
            data={'token':args.token,'partition':args.partition})
        response.raise_for_status()
        ticket=response.json()
        if ticket.get('mode')!='prepared':raise RuntimeError('Prepared downloads required for checksum proof')
        deadline=time.monotonic()+480
        while True:
            response=client.get(site+ticket['status_url'])
            response.raise_for_status()
            state=response.json()
            print(json.dumps({'event':'samsung_image_preparation',**state}),flush=True)
            if state.get('state')=='ready':break
            if state.get('state') in ('failed','cancelled') or time.monotonic()>deadline:raise RuntimeError('Samsung image preparation failed')
            time.sleep(5)
        digest=hashlib.sha256();total=0;magic=b''
        with client.stream('GET',site+ticket['download_url']) as response:
            response.raise_for_status()
            expected=response.headers.get('X-FMR-SHA256')
            length=int(response.headers['Content-Length'])
            for chunk in response.iter_bytes(65536):
                if not magic:magic=chunk[:8]
                total+=len(chunk);digest.update(chunk)
                if total>256*1024**2:raise RuntimeError('Oversized test image')
        if total!=length or digest.hexdigest()!=expected or not magic.startswith((b'ANDROID!',b'VNDRBOOT')):
            raise RuntimeError('Image size, checksum or magic mismatch')
        print(json.dumps({'event':'samsung_image_verified','token':args.token,'partition':args.partition,
            'bytes':total,'sha256':digest.hexdigest(),'disk_bytes':0}),flush=True)
    return 0

if __name__=='__main__':raise SystemExit(main())
