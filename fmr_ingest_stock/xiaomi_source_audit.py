"""Read-only catalogue diagnostics with a bounded disposable response-header cache."""
import argparse
import json
import os
import hashlib
import time
import tempfile
from pathlib import Path
import sqlite3
from urllib.parse import urlsplit
import httpx
from .history import official
from .cdn_rewriter import rewrite_to_fast_cdn
from .xiaomi_variants import filename
from .catalogue_store import KIND,digest
from .xiaomi_identity import corrected_release
from .xiaomi_devices import physical_code
from collections import Counter

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--network',action='store_true')
    parser.add_argument('--full',action='store_true')
    args=parser.parse_args(argv)
    path=Path(os.environ.get('FMR_CATALOGUE_STATE_DIR','/var/lib/findmyrom/catalogue-v3'))/'xiaomi.sqlite'
    cache_path=path.with_name('xiaomi-source-heads.json')
    try:cache=json.loads(cache_path.read_text()) if cache_path.stat().st_size<131072 else {}
    except (OSError,ValueError):cache={}
    changed=False
    receipt_path=path.with_name('xiaomi-snapshot-audit.json')
    try:receipt=json.loads(receipt_path.read_text())
    except (OSError,ValueError):receipt={}
    full_check=bool(args.full or receipt.get('sha')!=os.environ.get('GITHUB_SHA') or receipt.get('checked_at',0)+86400<time.time())
    conn=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)
    issues=0
    try:
        states=Counter();ready=0;total=0;mismatches=0;invalid=0
        for source_key,body in conn.execute("SELECT source_target,last_cursor_val FROM crawler_sources WHERE device_codename='__global__' AND source_kind=? AND source_target GLOB 'v2:xiaomi:release:*' LIMIT ?",(KIND,-1 if full_check else 0)):
            value=json.loads(body);state=value.get('status','pending');states[state]+=1;total+=1
            ready+=state=='resolved' and not value.get('published')
            if state in ('excluded','superseded'):continue
            try:
                release=value['release'];device=value['device'];url=release.get('download_url','')
                if not official(url):invalid+=1;continue
                corrected=corrected_release(device,release)
                owner=physical_code(device['upstream_id'],filename(url))
                identity=[owner,corrected.get('version'),corrected.get('region'),corrected.get('kind')]
                if corrected.get('variant_id'):identity.append(corrected['variant_id'])
                mismatches+=source_key!='v2:xiaomi:release:'+digest(identity)
            except (KeyError,TypeError,ValueError):invalid+=1
        def checkpoint(key):
            found=conn.execute("SELECT last_cursor_val FROM crawler_sources WHERE device_codename='__global__' AND source_kind=? AND source_target=?",(KIND,'v2:xiaomi:'+key)).fetchone()
            return json.loads(found[0]) if found else {}
        counts=checkpoint('counts');consistent=(counts.get('releases',{})==dict(states) and counts.get('ready',0)==ready) if full_check else None
        marker=checkpoint('history_identity_repair');control=checkpoint('control')
        model_count=conn.execute("SELECT count(*) FROM crawler_sources WHERE device_codename='__global__' AND source_kind=? AND source_target GLOB 'v2:xiaomi:model:*'",(KIND,)).fetchone()[0]
        issues=mismatches+invalid+int(consistent is False)
        print(json.dumps({'event':'xiaomi_snapshot_invariants','models':model_count,'release_slots':total if full_check else sum(counts.get('releases',{}).values()),'full_check':full_check,'checked_rows':total,'active_identity_mismatches':mismatches if full_check else None,'invalid_descriptors':invalid if full_check else None,'counts_consistent':consistent,
                          'repair_revision':marker.get('revision'),'repair_done':marker.get('done'),'reconciled_conflicts':marker.get('reconciled_conflicts',0),'source_conflicts_reconciled':marker.get('source_conflicts_reconciled',0),'code_sha':control.get('last_code_sha'),'states':dict(states) if full_check else counts.get('releases',{}),'last_full_check':receipt.get('checked_at')}),flush=True)
        rows=conn.execute("""SELECT source_target,last_cursor_val FROM crawler_sources
            WHERE device_codename='__global__' AND source_kind=? AND source_target GLOB 'v2:xiaomi:release:*'
            AND json_extract(last_cursor_val,'$.status') IN ('blocked','failed','unavailable')
            AND source_target>? ORDER BY source_target LIMIT 20""",(KIND,receipt.get('review_cursor',''))).fetchall()
        if not rows and receipt.get('review_cursor'):
            rows=conn.execute("""SELECT source_target,last_cursor_val FROM crawler_sources
                WHERE device_codename='__global__' AND source_kind=? AND source_target GLOB 'v2:xiaomi:release:*'
                AND json_extract(last_cursor_val,'$.status') IN ('blocked','failed','unavailable')
                ORDER BY source_target LIMIT 20""",(KIND,)).fetchall()
        receipt['review_cursor']=rows[-1][0] if rows else ''
        heads=0
        print(json.dumps({'event':'xiaomi_source_review','rows':len(rows),'limit':20}),flush=True)
        with httpx.Client(timeout=8,follow_redirects=False) as client:
            for key,raw in rows:
                v=json.loads(raw);candidates=[v['release']]
                for ref in v.get('identity_review_sources',[])[:3]:
                    found=conn.execute("SELECT last_cursor_val FROM crawler_sources WHERE device_codename='__global__' AND source_kind=? AND source_target=?",(KIND,ref)).fetchone()
                    if found:candidates.append(json.loads(found[0])['record']['release'])
                model_key='v2:xiaomi:model:'+digest(v['device']['upstream_id'])
                found=conn.execute("SELECT last_cursor_val FROM crawler_sources WHERE device_codename='__global__' AND source_kind=? AND source_target=?",(KIND,model_key)).fetchone()
                model=json.loads(found[0]) if found else {}
                for conflict in model.get('provenance',{}).get('conflicts',[]):
                    ck=conflict.get('key',[])
                    if len(ck)>2 and ck[2]==v['release']['version']:
                        alternate=conflict.get('alternate',{})
                        if alternate.get('link'):candidates.append({'download_url':alternate['link'],'md5':alternate.get('md5'),'size':alternate.get('size')})
                candidates=list({r.get('download_url'):r for r in candidates}.values())[:5]
                result={'event':'xiaomi_source_conflict','device':v['device']['upstream_id'],'error':v.get('error'),'files':[]}
                for release in candidates:
                    url=release.get('download_url','')
                    if not official(url):continue
                    f={'filename':filename(url),'md5':release.get('md5'),'size':release.get('size')}
                    cache_key=hashlib.sha256(url.encode()).hexdigest()
                    saved=cache.get(cache_key,{})
                    if args.network and 'original_status' in saved.get('head',{}) and saved.get('checked_at',0)+900>time.time():f['head']=saved['head']
                    elif args.network and heads<20:
                        try:
                            heads+=1;response=client.head(url)
                            original_status=response.status_code
                            fast=rewrite_to_fast_cdn('xiaomi',url)
                            if response.status_code in (403,404) and fast!=url and heads<20:
                                heads+=1;response=client.head(fast)
                            f['head']={'status':response.status_code,'type':response.headers.get('content-type'),'length':response.headers.get('content-length'),'etag':response.headers.get('etag'),'original_status':original_status}
                        except httpx.HTTPError as exc:f['head']={'error':type(exc).__name__}
                        cache[cache_key]={'checked_at':int(time.time()),'head':f['head']};changed=True
                    result['files'].append(f)
                print(json.dumps(result,ensure_ascii=False),flush=True)
    finally:conn.close()
    if changed:
        cache=dict(sorted(cache.items(),key=lambda item:item[1].get('checked_at',0),reverse=True)[:128])
        with tempfile.NamedTemporaryFile(mode='w',dir=path.parent,delete=False) as handle:
            json.dump(cache,handle);temporary=handle.name
        Path(temporary).replace(cache_path)
    if full_check and not issues:receipt.update(sha=os.environ.get('GITHUB_SHA'),checked_at=int(time.time()))
    if not issues:
        with tempfile.NamedTemporaryFile(mode='w',dir=path.parent,delete=False) as handle:
            json.dump(receipt,handle);temporary=handle.name
        Path(temporary).replace(receipt_path)
    return 1 if issues else 0

if __name__=='__main__':raise SystemExit(main())
