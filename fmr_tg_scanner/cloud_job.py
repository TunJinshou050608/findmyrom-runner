"""Small resumable batches, with state in the existing private Vault (not public artifacts)."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import tempfile

import httpx
from .storage import SQLiteDB
from .crawler import scan_channel
from .html_parser import channel_name

TAG='tg01-private-checkpoints-v1'
MAX_STATE_BYTES=128*1024*1024


def log(event,**values):print(json.dumps({'event':event,**values},sort_keys=True),flush=True)


class PrivateCheckpoint:
    def __init__(self,client,repo,channel):
        self.client=client;self.repo=repo;self.channel=channel;self.name='tg01-'+channel+'.sqlite'
        self.api='https://api.github.com/repos/'+repo
    def release(self):
        r=self.client.get(self.api+'/releases/tags/'+TAG)
        if r.status_code==404:
            r=self.client.post(self.api+'/releases',json={'tag_name':TAG,'name':'Private TG01 scan checkpoints','body':'Private metadata only. No firmware files or bot credentials.'})
        r.raise_for_status();return r.json()
    def restore(self,path):
        info=self.release();assets=[];page=1
        while True:
            r=self.client.get(self.api+f"/releases/{info['id']}/assets",params={'per_page':100,'page':page});r.raise_for_status()
            batch=r.json();assets.extend(batch)
            if len(batch)<100:break
            page+=1
        valid=[]
        for asset in assets:
            if re.fullmatch(re.escape(self.name)+r'\.[a-f0-9]{64}',asset['name']):valid.append(asset)
        if not valid:return None,info,assets
        # Upload new immutable generation *before* deleting old ones. Any crash
        # retains at least one complete checkpoint. Never silently restart corrupt state.
        latest=max(valid,key=lambda a:a['id']);digest=latest['name'].rsplit('.',1)[1]
        headers={'Accept':'application/octet-stream'}
        with self.client.stream('GET',self.api+'/releases/assets/'+str(latest['id']),headers=headers) as response:
            if response.status_code in (301,302,307):
                url=response.headers.get('Location','')
                from urllib.parse import urlsplit
                u=urlsplit(url)
                if u.scheme!='https' or not u.hostname or not u.hostname.endswith('.githubusercontent.com'):raise ValueError('Unexpected checkpoint redirect')
                # Credential-free download from the signed GitHub asset URL.
                with httpx.stream('GET',url,timeout=60,follow_redirects=False) as download:
                    download.raise_for_status();self._copy(download,path)
            else:
                response.raise_for_status();self._copy(response,path)
        if hashlib.sha256(path.read_bytes()).hexdigest()!=digest:raise ValueError('Checkpoint hash mismatch')
        return digest,info,assets
    def _copy(self,response,path):
        total=0
        with path.open('wb') as out:
            for chunk in response.iter_bytes():
                total+=len(chunk)
                if total>MAX_STATE_BYTES:raise ValueError('Checkpoint size limit')
                out.write(chunk)
    def save(self,path,prior,info,assets):
        if path.stat().st_size>MAX_STATE_BYTES:raise ValueError('Checkpoint size limit')
        digest=hashlib.sha256(path.read_bytes()).hexdigest()
        if digest==prior:return False
        name=self.name+'.'+digest
        url=f"https://uploads.github.com/repos/{self.repo}/releases/{info['id']}/assets"
        with path.open('rb') as source:
            r=self.client.post(url,params={'name':name},content=source,headers={'Content-Type':'application/octet-stream','Content-Length':str(path.stat().st_size)})
        r.raise_for_status()
        for old in assets:
            if old['name'].startswith(self.name+'.'):
                removed=self.client.delete(self.api+'/releases/assets/'+str(old['id']));removed.raise_for_status()
        return True


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--channel',required=True);p.add_argument('--default-codename')
    p.add_argument('--max-pages',type=int,default=20);p.add_argument('--direction',choices=['both','backfill','incremental'],default='both');args=p.parse_args(argv)
    if os.environ.get('GITHUB_ACTIONS')!='true':p.error('Cloud jobs must run on GitHub Actions')
    if not 1<=args.max_pages<=100:p.error('Cloud batch limit is 1–100 pages')
    def terminate(_signum,_frame):raise InterruptedError('Cloud job stopped')
    signal.signal(signal.SIGTERM,terminate)
    channel=channel_name(args.channel);repo=os.environ['GH_VAULT_REPO']
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',repo):raise ValueError('Invalid private state repository')
    # Do not ever write state into a public repository, even with a misconfigured secret.
    with httpx.Client(timeout=60,follow_redirects=False,headers={'Authorization':'Bearer '+os.environ['GH_VAULT_TOKEN'],'Accept':'application/vnd.github+json'}) as client:
        check=client.get('https://api.github.com/repos/'+repo);check.raise_for_status()
        if check.json().get('private') is not True:raise ValueError('Checkpoint repository must be private')
        with tempfile.TemporaryDirectory(prefix='fmr-tg01-') as folder:
            path=Path(folder)/'state.sqlite';checkpoint=PrivateCheckpoint(client,repo,channel)
            digest,info,assets=checkpoint.restore(path);db=SQLiteDB(path)
            from .schema import initialize
            initialize(db)
            if digest:
                if db.query('PRAGMA integrity_check')[0]['integrity_check']!='ok':raise ValueError('Checkpoint SQLite integrity failure')
            try:
                stats=scan_channel(db,channel,args.default_codename,args.max_pages,args.direction,False,0,
                    on_page=lambda data:log('page_committed',**data))
                log('batch_complete',**stats)
            finally:
                db.close()
                # A failed HTTP page still uploads prior successful page commits.
                changed=checkpoint.save(path,digest,info,assets);log('private_checkpoint_saved',changed=changed)
    return 0


if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as exc:
        log('batch_stopped',error_type=type(exc).__name__);raise SystemExit(1)
