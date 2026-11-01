"""Encrypted task-side catalogue snapshots; collectors never connect to D1.

Only metadata is stored. Domain-separated AEAD keeps public Actions artifacts
opaque. A new job restores the last uploaded snapshot before doing any work.
"""
import argparse
from contextlib import ExitStack
import hashlib
import io
import json
import os
from pathlib import Path
import time
import zipfile
import zlib

import httpx
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fmr_core.d1_client import D1Client
from .catalogue_store import Store, KIND
from .device_sync import ensure_base_brands_and_anchor
from . import vendor_catalogue as job

ROOT=Path('catalogue-state')
PREFIX='catalogue-v3-'
BASELINE=PREFIX+'baseline'
MAX_SNAPSHOT=256*1024*1024


def seal(data,scope):
    key=hashlib.sha256(b'findmyrom/catalogue-state/v3\0'+bytes.fromhex(os.environ['SIGN_PRIVATE_KEY_HEX'])).digest()
    nonce=os.urandom(12)
    return b'FMR3'+nonce+AESGCM(key).encrypt(nonce,zlib.compress(data),scope.encode())


def unseal(data,scope):
    if data[:4]!=b'FMR3':raise ValueError('Unknown encrypted snapshot format')
    key=hashlib.sha256(b'findmyrom/catalogue-state/v3\0'+bytes.fromhex(os.environ['SIGN_PRIVATE_KEY_HEX'])).digest()
    compressed=AESGCM(key).decrypt(data[4:16],data[16:],scope.encode())
    dec=zlib.decompressobj();body=dec.decompress(compressed,MAX_SNAPSHOT+1)
    if len(body)>MAX_SNAPSHOT or not dec.eof or dec.unused_data or dec.unconsumed_tail:raise ValueError('Invalid snapshot size')
    return body


class Artifacts:
    def __init__(self):
        self.base='https://api.github.com/repos/'+os.environ['GITHUB_REPOSITORY']
        self._baseline_archive=None
        self.client=httpx.Client(timeout=120,headers={'Authorization':'Bearer '+os.environ['GH_WORKFLOW_TOKEN'],'Accept':'application/vnd.github+json'})

    def close(self):self.client.close()

    def list(self,name):
        # Server-side name filter avoids scanning artifacts from other jobs.
        r=self.client.get(self.base+'/actions/artifacts',params={'name':name,'per_page':100});r.raise_for_status()
        return sorted([a for a in r.json()['artifacts'] if not a['expired']],key=lambda a:a['id'],reverse=True)

    def restore(self,scope,path,baseline=True):
        items=self.list(PREFIX+scope);using_baseline=False
        if not items and baseline:items=self.list(BASELINE);using_baseline=bool(items)
        if not items:return False
        # The common migration archive contains every vendor. Download it only
        # once per publishing job instead of once for each vendor.
        cached=self._baseline_archive
        if using_baseline and cached and cached[0]==items[0]['id']:
            payload=cached[1]
        else:
            # Never silently fall back after authentication/corruption errors.
            response=self.client.get(self.base+'/actions/artifacts/'+str(items[0]['id'])+'/zip')
            if response.status_code in (301,302,303,307,308):
                # Artifact object storage receives no GitHub bearer token.
                with httpx.Client(timeout=120,follow_redirects=True) as download:
                    response=download.get(response.headers['Location']);response.raise_for_status()
            response.raise_for_status();payload=response.content
            if using_baseline:self._baseline_archive=(items[0]['id'],payload)
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            name=scope+'.sqlite.enc'
            entry=archive.getinfo(name)
            if entry.file_size>MAX_SNAPSHOT:raise ValueError('Oversize snapshot')
            body=unseal(archive.read(name),scope)
        path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(body)
        return items[0]['id']

    def prune(self,scope):
        # Run only AFTER a successful upload. Keep three independent recovery copies.
        for item in self.list(PREFIX+scope)[3:]:
            r=self.client.delete(self.base+'/actions/artifacts/'+str(item['id']));r.raise_for_status()


def open_local(path):
    db=D1Client(local_sqlite_path=str(path))
    db.execute_script(Path(__file__).parents[1].joinpath('schema.sql').read_text())
    ensure_base_brands_and_anchor(db)
    return db


def encrypt_file(scope,path):
    target=ROOT/'upload'/(scope+'.sqlite.enc');target.parent.mkdir(parents=True,exist_ok=True)
    target.write_bytes(seal(path.read_bytes(),scope))


def collect(vendor,operation='incremental',seconds=420):
    ROOT.mkdir(exist_ok=True);path=ROOT/(vendor+'.sqlite')
    artifacts=Artifacts()
    try:
        if not artifacts.restore(vendor,path):
            # A missing baseline is not permission to refetch all history.
            raise RuntimeError('MigrationBaselineMissing')
        db=open_local(path)
        deadline=time.monotonic()+seconds
        result=0
        try:
            store=Store(db,vendor)
            while time.monotonic()<deadline:
                control=store.get(store.key('control')) or {}
                cycle=control.get('cycle','') if control.get('state') in ('working','paused') else ''
                result=job.run(db,vendor,mode='audit',operation=operation,cycle=cycle,
                    sequence=control.get('batch',1),dispatch_next=False,time_budget=max(0,deadline-time.monotonic()))
                control=store.get(store.key('control')) or {}
                if result or control.get('state')!='working':break
        finally:db.close();encrypt_file(vendor,path)
        print('采集检查点已加密；本任务 D1 请求为 0。',flush=True)
        return result
    finally:artifacts.close()


def next_batch(vendor):
    path=ROOT/(vendor+'.sqlite')
    if not path.exists():return
    db=open_local(path)
    try:
        store=Store(db,vendor);control=store.get(store.key('control')) or {}
        if control.get('state')=='working':job.dispatch(control,vendor)
    finally:db.close()


def baseline():
    """One indexed, read-only migration of old catalogue state; never delete D1 rows."""
    from .d1_budget import BudgetDB
    ROOT.mkdir(exist_ok=True)
    db=BudgetDB(cf_account_id=os.environ['CF_ACCOUNT_ID'],cf_api_token=os.environ['CF_API_TOKEN'],cf_database_id=os.environ['CF_D1_DATABASE_ID'])
    locals={}
    with ExitStack() as stack:
        stack.callback(db.close)
        if not db.acquire():raise RuntimeError('MigrationDailyBudgetUnavailable')
        for vendor in job.VENDORS:
            path=ROOT/(vendor+'.sqlite');local=open_local(path);locals[vendor]=local;stack.callback(local.close)
        try:
            for kind in ('oem_catalogue',KIND):
                cursor=''
                while True:
                    rows=db.query("SELECT source_target,last_cursor_val,last_checked_at FROM crawler_sources WHERE device_codename='__global__' AND source_kind=? AND source_target>? ORDER BY source_target LIMIT 100",(kind,cursor))
                    if not rows:break
                    for row in rows:
                        value=json.loads(row['last_cursor_val'])
                        vendor=(row['source_target'].split(':')[1] if kind==KIND and row['source_target'].startswith('v2:') else value.get('device',{}).get('vendor'))
                        if vendor not in locals:continue
                        locals[vendor].execute("INSERT OR REPLACE INTO crawler_sources(device_codename,source_kind,source_target,last_cursor_val,last_checked_at) VALUES ('__global__',?,?,?,?)",(kind,row['source_target'],row['last_cursor_val'],row['last_checked_at']))
                    cursor=rows[-1]['source_target']
                    print('接续旧检查点：已读取 '+str(db.rows_read)+' 行；不重新抓取。',flush=True)
            for vendor,local in locals.items():
                # Convert legacy work entirely locally, once.
                store=Store(local,vendor)
                with job.Status(vendor) as status:
                    while not job.migrate(store,status,limit=500):pass
                # Collector state is now audit-only. Ignore obsolete continuation IDs.
                key=store.key('control');control=store.get(key)
                if control:control.update(mode='audit',state='paused');store.put(key,control)
        finally:
            for local in locals.values():local.close()
        for vendor in job.VENDORS:encrypt_file(vendor,ROOT/(vendor+'.sqlite'))


def prepare():
    """Verify operator-disabled old writers; job tokens never need admin access."""
    artifacts=Artifacts()
    obsolete={'history_sweep.yml','batch_extract.yml','oem_catalogue.yml','quota_resume.yml','sony_bundle.yml'}
    targets=obsolete|{'catalogue_'+v+'.yml' for v in job.VENDORS}
    try:
        for workflow in obsolete:
            r=artifacts.client.get(artifacts.base+'/actions/workflows/'+workflow)
            r.raise_for_status()
            if not r.json().get('state','').startswith('disabled'):
                raise RuntimeError('OperatorMustDisableLegacyWorkflow:'+workflow)
        for state in ('in_progress','queued'):
            r=artifacts.client.get(artifacts.base+'/actions/runs',params={'status':state,'per_page':100});r.raise_for_status()
            for run in r.json()['workflow_runs']:
                if run.get('path','').split('/')[-1] not in targets:continue
                r=artifacts.client.post(artifacts.base+'/actions/runs/'+str(run['id'])+'/cancel')
                if r.status_code not in (202,409):r.raise_for_status()
        print('旧D1写入入口已暂停；取消请求已提交，保留全部检查点。未轮询运行状态。',flush=True)
    finally:artifacts.close()


def main(argv=None):
    parser=argparse.ArgumentParser()
    parser.add_argument('command',choices=['prepare','baseline','collect','next','prune'])
    parser.add_argument('--vendor',choices=job.VENDORS)
    parser.add_argument('--operation',default='incremental',choices=['incremental','retry_failed','refresh_sources'])
    args=parser.parse_args(argv)
    if args.command=='prepare':prepare();return 0
    if args.command=='baseline':baseline();return 0
    if not args.vendor:parser.error('--vendor required')
    if args.command=='collect':return collect(args.vendor,args.operation)
    if args.command=='next':next_batch(args.vendor)
    if args.command=='prune':
        a=Artifacts()
        try:a.prune(args.vendor)
        finally:a.close()
    return 0


if __name__=='__main__':raise SystemExit(main())
