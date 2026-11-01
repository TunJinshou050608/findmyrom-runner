"""Encrypted task-side catalogue snapshots; collectors never connect to D1.

Only metadata is stored. Domain-separated AEAD keeps public Actions artifacts
opaque. A new job restores the last uploaded snapshot before doing any work.
"""
import argparse
from contextlib import ExitStack,contextmanager,closing
import hashlib
import fcntl
import io
import json
import os
import sqlite3
from pathlib import Path
import time
import tempfile
import zipfile
import zlib

import httpx
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fmr_core.d1_client import D1Client
from .catalogue_store import Store, KIND
from .device_sync import ensure_base_brands_and_anchor
from . import vendor_catalogue as job

ROOT=Path(os.environ.get('FMR_CATALOGUE_STATE_DIR','catalogue-state'))
PREFIX='catalogue-v3-'
BASELINE=PREFIX+'baseline'
# Xiaomi history plus cached metadata legitimately exceeds the old 256 MiB
# decoded limit. Keep separate hard caps for the encrypted transfer and SQLite.
MAX_ENCRYPTED_SNAPSHOT=256*1024*1024
MAX_SNAPSHOT=1024*1024*1024


def seal(data,scope):
    if len(data)>MAX_SNAPSHOT:raise ValueError('Invalid snapshot size')
    key=hashlib.sha256(b'findmyrom/catalogue-state/v3\0'+bytes.fromhex(os.environ['SIGN_PRIVATE_KEY_HEX'])).digest()
    nonce=os.urandom(12)
    result=b'FMR3'+nonce+AESGCM(key).encrypt(nonce,zlib.compress(data),scope.encode())
    if len(result)>MAX_ENCRYPTED_SNAPSHOT:raise ValueError('Oversize snapshot')
    return result


def unseal(data,scope):
    if len(data)>MAX_ENCRYPTED_SNAPSHOT:raise ValueError('Oversize snapshot')
    if data[:4]!=b'FMR3':raise ValueError('Unknown encrypted snapshot format')
    key=hashlib.sha256(b'findmyrom/catalogue-state/v3\0'+bytes.fromhex(os.environ['SIGN_PRIVATE_KEY_HEX'])).digest()
    compressed=AESGCM(key).decrypt(data[4:16],data[16:],scope.encode())
    dec=zlib.decompressobj();body=dec.decompress(compressed,MAX_SNAPSHOT+1)
    if len(body)>MAX_SNAPSHOT or not dec.eof or dec.unused_data or dec.unconsumed_tail:raise ValueError('Invalid snapshot size')
    return body


@contextmanager
def state_lock(path,suffix):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    descriptor=os.open(str(path)+suffix,os.O_CREAT|os.O_RDWR,0o600)
    with os.fdopen(descriptor,'a') as handle:
        fcntl.flock(handle,fcntl.LOCK_EX)
        try:yield
        finally:fcntl.flock(handle,fcntl.LOCK_UN)


def checkpoint_stamp(path,*,artifact_id=None,writer_run_id=None):
    stamp=Path(path).with_suffix('.checkpoint.json')
    with state_lock(path,'.stamp.lock'):
        value=json.loads(stamp.read_text()) if stamp.exists() else {}
        if artifact_id is not None:value['artifact_id']=max(int(value.get('artifact_id',0)),int(artifact_id))
        if writer_run_id is not None:value['writer_run_id']=str(writer_run_id)
        with tempfile.NamedTemporaryFile(mode='w',dir=stamp.parent,delete=False) as temp:
            json.dump(value,temp);name=temp.name
        Path(name).replace(stamp)
    return value


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
        with state_lock(path,'.restore.lock'):
            return self._restore(scope,path,baseline)

    def _restore(self,scope,path,baseline=True):
        path=Path(path)
        if os.environ.get('FMR_CATALOGUE_LOCAL_STATE')=='true' and path.exists():
            # The fixed host owns these databases outside checkout cleanup. Do
            # not replace newer local transactions with an older remote snapshot.
            with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as local:
                table=local.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('crawler_sources','published') LIMIT 1").fetchone()
                if table is None:raise RuntimeError('InvalidLocalCatalogueState')
            stamp_path=path.with_suffix('.checkpoint.json')
            if not stamp_path.exists():raise RuntimeError('UntrackedLocalCatalogueState')
            stamp=json.loads(stamp_path.read_text());items=self.list(PREFIX+scope)
            latest=items[0] if items else None
            remote_run=(latest.get('workflow_run') or {}).get('id') if latest else None
            unchanged=not latest or latest['id']<=stamp.get('artifact_id',0)
            own_upload=bool(remote_run) and str(remote_run)==stamp.get('writer_run_id')
            if unchanged or own_upload:
                if latest:checkpoint_stamp(path,artifact_id=latest['id'])
                print(scope+': 接续本机持久状态；仅核对备份版本，不读取 D1 历史或下载整份快照。',flush=True)
                return 'local'
            # A previous/other runner produced a newer checkpoint. Fetch only
            # that changed file, rather than letting a stale local copy win.
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
            if entry.file_size>MAX_ENCRYPTED_SNAPSHOT:raise ValueError('Oversize snapshot')
            body=unseal(archive.read(name),scope)
        path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        with tempfile.NamedTemporaryFile(dir=path.parent,delete=False) as temp:
            temp.write(body);name=temp.name
        Path(name).replace(path)
        checkpoint_stamp(path,artifact_id=items[0]['id'],writer_run_id=(items[0].get('workflow_run') or {}).get('id',''))
        return items[0]['id']

    def prune(self,scope):
        # Run only AFTER a successful upload. Keep three independent recovery copies.
        for item in self.list(PREFIX+scope)[3:]:
            r=self.client.delete(self.base+'/actions/artifacts/'+str(item['id']));r.raise_for_status()


def open_local(path,initialize=True):
    db=D1Client(local_sqlite_path=str(path))
    if initialize:
        db.execute_script(Path(__file__).parents[1].joinpath('schema.sql').read_text())
        ensure_base_brands_and_anchor(db)
    return db


def encrypt_file(scope,path):
    target=ROOT/'upload'/(scope+'.sqlite.enc');target.parent.mkdir(parents=True,exist_ok=True)
    # Online backup produces a consistent SQLite image while another runner
    # reads/updates the shared database. Never copy live database bytes directly.
    with tempfile.TemporaryDirectory(dir=ROOT) as directory:
        copy=Path(directory)/'snapshot.sqlite'
        with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=30)) as source:
            with closing(sqlite3.connect(copy)) as destination:
                source.backup(destination)
                # Reclaim free pages from completed history without dropping
                # checkpoints/cache entries or mutating the live local database.
                destination.execute('VACUUM')
        target.write_bytes(seal(copy.read_bytes(),scope))
    checkpoint_stamp(path,writer_run_id=os.environ.get('GITHUB_RUN_ID','local'))


def collect(vendor,operation='incremental',seconds=420):
    if vendor in job.SUPPRESSED_VENDORS:
        print(vendor+': paused by user; no network or D1 requests.',flush=True);return 0
    ROOT.mkdir(parents=True,exist_ok=True,mode=0o700);path=ROOT/(vendor+'.sqlite')
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
    ROOT.mkdir(parents=True,exist_ok=True,mode=0o700)
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
    # The supported cloud extractor is independent of metadata migration. It is
    # not a legacy collector and must not be disabled/cancelled by bootstrap.
    obsolete={'history_sweep.yml','oem_catalogue.yml','quota_resume.yml','sony_bundle.yml'}
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
