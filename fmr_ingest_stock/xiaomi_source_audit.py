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
from .xiaomi_variants import filename
from .catalogue_store import KIND

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--network',action='store_true')
    args=parser.parse_args(argv)
    path=Path(os.environ.get('FMR_CATALOGUE_STATE_DIR','/var/lib/findmyrom/catalogue-v3'))/'xiaomi.sqlite'
    cache_path=path.with_name('xiaomi-source-heads.json')
    try:cache=json.loads(cache_path.read_text()) if cache_path.stat().st_size<131072 else {}
    except (OSError,ValueError):cache={}
    changed=False
    conn=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)
    try:
        rows=conn.execute("""SELECT source_target,last_cursor_val FROM crawler_sources
            WHERE device_codename='__global__' AND source_kind=? AND source_target GLOB 'v2:xiaomi:release:*'
            AND json_extract(last_cursor_val,'$.status') IN ('blocked','failed')
            ORDER BY source_target LIMIT 20""",(KIND,)).fetchall()
        print(json.dumps({'event':'xiaomi_source_review','rows':len(rows),'limit':20}),flush=True)
        with httpx.Client(timeout=8,follow_redirects=False) as client:
            for key,raw in rows:
                v=json.loads(raw);candidates=[v['release']]
                for ref in v.get('identity_review_sources',[])[:3]:
                    found=conn.execute("SELECT last_cursor_val FROM crawler_sources WHERE device_codename='__global__' AND source_kind=? AND source_target=?",(KIND,ref)).fetchone()
                    if found:candidates.append(json.loads(found[0])['record']['release'])
                result={'event':'xiaomi_source_conflict','device':v['device']['upstream_id'],'error':v.get('error'),'files':[]}
                for release in candidates:
                    url=release.get('download_url','')
                    if not official(url):continue
                    f={'filename':filename(url),'md5':release.get('md5'),'size':release.get('size')}
                    cache_key=hashlib.sha256(url.encode()).hexdigest()
                    saved=cache.get(cache_key,{})
                    if args.network and saved.get('checked_at',0)+900>time.time():f['head']=saved['head']
                    elif args.network:
                        try:
                            response=client.head(url)
                            f['head']={'status':response.status_code,'type':response.headers.get('content-type'),'length':response.headers.get('content-length'),'etag':response.headers.get('etag')}
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
    return 0

if __name__=='__main__':raise SystemExit(main())
