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
    parser.add_argument('--direct-only',action='store_true')
    parser.add_argument('--model',default='SM-S928B')
    parser.add_argument('--region',default='EUX')
    parser.add_argument('--version')
    args=parser.parse_args()
    if args.direct_only:
        from fmr_extractor.service import open_image
        job=dict(brand='samsung',model=args.model,region=args.region,version=args.version,partition=args.partition,
            package_type='samsung_encrypted',url='https://cloud-neofussvr.samsungmobile.com/firmware.enc4')
        try:
            with open_image(job) as (chunks,metrics,raw):
                first=next(chunks)
                print(json.dumps({'event':'samsung_exact_archive_probe','model':args.model,'bytes':len(first),'magic':first[:8].hex(),'upstream_bytes':raw.used}),flush=True)
        except Exception as exc:
            print(json.dumps({'event':'samsung_exact_archive_probe_failure','type':type(exc).__name__,'reason':str(exc)[:240]}),flush=True)
            raise
        return 0
    site='https://findmyrom.com'
    with httpx.Client(timeout=90,follow_redirects=False,trust_env=False) as client:
        deadline=time.monotonic()+660
        while True:
            caps=client.get('https://extract-origin.findmyrom.com/v1/capabilities')
            caps.raise_for_status()
            info=caps.json()
            print(json.dumps({'event':'samsung_vps_capabilities',**info}),flush=True)
            if info.get('samsung_full_stream') and info.get('samsung_exact_builds'):break
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
        # Normal package ticket follows the signed refresh link to a live decrypted ZIP.
        response=client.post(site+'/api/ticket',headers={'Origin':site},
            json={'token':args.token,'line':'1'})
        response.raise_for_status()
        download=response.json()['download_url']
        with client.stream('GET',site+download,follow_redirects=True) as response:
            response.raise_for_status()
            size=int(response.headers['Content-Length'])
            first=next(response.iter_bytes(65536))
            if not first.startswith(bytes([80,75,3,4])):raise RuntimeError('Decrypted ZIP signature missing')
            print(json.dumps({'event':'samsung_full_zip_stream_verified','bytes_probed':len(first),
                'download_total':size,'disk_bytes':0,'completed_full_package':False}),flush=True)
    return 0

if __name__=='__main__':raise SystemExit(main())
