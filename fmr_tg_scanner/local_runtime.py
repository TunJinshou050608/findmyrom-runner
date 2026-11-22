"""Four independent local TG frontiers, one snapshot auditor, one D1 outbox."""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import time
import traceback

from .runtime_status import record_business_failure, record_business_success
from .trial import atomic_json

WORKERS = 4


def partition(code, workers=WORKERS):
    return int.from_bytes(hashlib.sha256(code.encode()).digest()[:8], 'big') % workers


@contextmanager
def lock(root, name):
    root=Path(root);root.mkdir(parents=True,exist_ok=True)
    with (root/(name+'.lock')).open('a') as handle:
        fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        try:yield
        finally:fcntl.flock(handle,fcntl.LOCK_UN)


def backup(source, target):
    target=Path(target);target.parent.mkdir(parents=True,exist_ok=True)
    with sqlite3.connect(Path(source).resolve().as_uri()+'?mode=ro',uri=True,timeout=30) as src:
        with sqlite3.connect(target) as dst:
            src.backup(dst)


def initialize(root, source):
    root=Path(root);source=Path(source)
    with lock(root,'initialize'):
        if (root/'initialized.json').exists():return
        if not (source/'search-v3/search.sqlite').exists():
            raise RuntimeError('Existing TG checkpoint must be migrated before starting')
        legacy=root/'legacy'
        legacy.mkdir(exist_ok=True)
        shutil.copytree(source/'search-v3',legacy/'search-v3',dirs_exist_ok=True)
        backup(source/'search-v3/search.sqlite',legacy/'search-v3/search.sqlite')
        for index in range(WORKERS):
            worker=root/'workers'/str(index);worker.mkdir(parents=True,exist_ok=True)
            db_path=worker/'search-v3/search.sqlite'
            backup(legacy/'search-v3/search.sqlite',db_path)
            with sqlite3.connect(db_path) as db:
                for table in ('tg_search_records','tg_search_tasks'):
                    devices=[row[0] for row in db.execute('SELECT DISTINCT device FROM '+table)]
                    for code in devices:
                        if partition(code)!=index:db.execute('DELETE FROM '+table+' WHERE device=?',(code,))
            # The common immutable legacy logs remain available to the auditor.
            atomic_json(worker/'tg_search_config.json',{'max_requests':100,'max_seconds':360,'max_tasks_per_device':24,'target_batch_size':8})
        atomic_json(root/'initialized.json',{'workers':WORKERS,'migrated_at':time.time(),'state_backend':'local_container','github_state_writes':False})
        print(json.dumps({'event':'local_TG_initialized','workers':WORKERS,'path':str(root),'github_state_writes':False}),flush=True)


def clean_once(root):
    """SQLite backups isolate the auditor from all four live writers."""
    from .search_store import SearchStore
    from .quality_audit import audit
    root=Path(root)
    if not (root/'initialized.json').exists():return {'status':'waiting_for_migration'}
    clean=root/'clean';search=clean/'search-v3';search.mkdir(parents=True,exist_ok=True)
    snapshots=clean/'snapshots';snapshots.mkdir(exist_ok=True)
    sources=[root/'legacy/search-v3/search.sqlite']+[root/'workers'/str(i)/'search-v3/search.sqlite' for i in range(WORKERS)]
    if not (search/'search.sqlite').exists():backup(sources[0],search/'search.sqlite')
    logs=search/'logs';logs.mkdir(exist_ok=True)
    for path in (root/'legacy/search-v3/logs').glob('*.jsonl'):
        target=logs/path.name
        if not target.exists():target.symlink_to(path.resolve())
    fingerprint=hashlib.sha256()
    store=SearchStore(search/'search.sqlite')
    try:
        store.execute('CREATE TABLE IF NOT EXISTS local_merge_versions(key TEXT PRIMARY KEY,version REAL NOT NULL)')
        versions={r['key']:r['version'] for r in store.query('SELECT * FROM local_merge_versions')}
        for index,source in enumerate(sources):
            snap=snapshots/(str(index)+'.sqlite');backup(source,snap)
            with sqlite3.connect(snap) as db:
                db.row_factory=sqlite3.Row
                rows=[dict(row) for row in db.execute('SELECT * FROM tg_search_records ORDER BY key')]
                fingerprint.update(json.dumps(rows,sort_keys=True).encode())
                for row in rows:
                    if row['updated']<=versions.get(row['key'],-1):continue
                    keys=list(row)
                    store.execute_batch([
                        ('INSERT INTO tg_search_records('+','.join(keys)+') VALUES ('+','.join('?' for _ in keys)+') ON CONFLICT(key) DO UPDATE SET '+','.join(k+'=excluded.'+k for k in keys if k!='key'),tuple(row[k] for k in keys)),
                        ('INSERT INTO local_merge_versions VALUES (?,?) ON CONFLICT(key) DO UPDATE SET version=excluded.version',(row['key'],row['updated']))])
                    versions[row['key']]=row['updated']
                for row in db.execute("SELECT key,value FROM tg_search_meta WHERE key LIKE 'profile:%' OR key='device_aliases_v3'"):
                    store.execute('INSERT INTO tg_search_meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(row['key'],row['value']))
                tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if 'tg_raw_messages' in tables:
                    raw_rows=[dict(r) for r in db.execute('SELECT * FROM tg_raw_messages')]
                    if raw_rows:
                        rcols=list(raw_rows[0])
                        r_sql=(
                            'INSERT INTO tg_raw_messages('+','.join(rcols)+') VALUES ('+','.join('?' for _ in rcols)+') '
                            'ON CONFLICT(post_key) DO UPDATE SET '+','.join(c+'=excluded.'+c for c in rcols if c!='post_key')+' '
                            'WHERE tg_raw_messages.content_revision_hash!=excluded.content_revision_hash '
                            'OR tg_raw_messages.last_observed_at<excluded.last_observed_at'
                        )
                        store.execute_batch([(r_sql,tuple(rr[c] for c in rcols)) for rr in raw_rows])
                if 'tg_cleaning_queue' in tables:
                    q_rows=[dict(r) for r in db.execute("SELECT * FROM tg_cleaning_queue WHERE status='pending'")]
                    if q_rows:
                        qcols=list(q_rows[0])
                        q_sql=(
                            'INSERT INTO tg_cleaning_queue('+','.join(qcols)+') VALUES ('+','.join('?' for _ in qcols)+') '
                            'ON CONFLICT(post_key) DO UPDATE SET '
                            'content_revision_hash=excluded.content_revision_hash,shared_body_hash=excluded.shared_body_hash,'
                            "status='pending',attempts=0,next_retry_at=0,last_error=NULL,updated_at=excluded.updated_at "
                            'WHERE tg_cleaning_queue.content_revision_hash!=excluded.content_revision_hash'
                        )
                        store.execute_batch([(q_sql,tuple(qr[c] for c in qcols)) for qr in q_rows])
    finally:store.close()
    fingerprint.update(('b01-grounded-v1:'+os.getenv('FMR_TG_GROUNDED_CLEANER','false')).encode())
    digest=fingerprint.hexdigest();last=clean/'source-fingerprint.json'
    if last.exists() and json.loads(last.read_text())['sha256']==digest:
        return {'status':'unchanged_sources','sha256':digest}
    # Every audit is immutable, including across repeated cycles of one Actions run.
    run_id=str(time.time_ns());original=os.environ.get('GITHUB_RUN_ID')
    os.environ['GITHUB_RUN_ID']=run_id
    try:out=audit(clean)
    except Exception as exc:
        if type(exc).__name__=='DeferredAudit':
            return {'status':'deferred','reason':str(exc)}
        raise
    finally:
        if original is None:os.environ.pop('GITHUB_RUN_ID',None)
        else:os.environ['GITHUB_RUN_ID']=original
    atomic_json(last,{'sha256':digest,'audit_run_id':run_id})
    result={'status':'audit_ready','audit_run_id':run_id,'report':json.loads((out/'report.json').read_text())}
    atomic_json(root/'clean-latest.json',result)
    return result


def dispatch(workflow, inputs=None):
    import httpx
    token=os.environ.get('GH_WORKFLOW_TOKEN');repo=os.environ.get('GITHUB_REPOSITORY')
    if not token or not repo:return
    with httpx.Client(timeout=30,trust_env=False) as client:
        r=client.post('https://api.github.com/repos/'+repo+'/actions/workflows/'+workflow+'/dispatches',
                      headers={'Authorization':'Bearer '+token,'Accept':'application/vnd.github+json'},
                      json={'ref':'main','inputs':inputs or {}})
        r.raise_for_status()


def loop(root, role, worker=0, duration=6000, period=600):
    root=Path(root)
    with lock(root,role+('-'+str(worker) if role=='fetch' else '')):
        deadline=time.monotonic()+duration;cycle=0
        while time.monotonic()<deadline:
            started=time.monotonic()
            try:
                if not (root/'initialized.json').exists():
                    result={'status':'waiting_for_migration'}
                elif role=='fetch':
                    from .search_watch import produce
                    os.environ['FMR_TG_CYCLE']=str(worker)+'-'+str(cycle)
                    result=produce(root/'workers'/str(worker),shard=worker,shards=WORKERS)
                    has_new=int(result.get('new_qualified_products') or result.get('packages') or 0)>0
                    record_business_success(root,'fetch',worker=worker,has_new_items=has_new,summary={'status':result.get('status',result.get('activity','ok')),'new_qualified_products':result.get('new_qualified_products',0)})
                else:
                    result=clean_once(root)
                    has_new=result.get('status')=='audit_ready'
                    record_business_success(root,'clean',worker=None,has_new_items=has_new,summary={'status':result.get('status')})
                    if has_new:dispatch('catalogue_publish.yml',{'scope':'tg'})
                atomic_json(root/(role+'-'+str(worker)+'-latest.json'),{'at':time.time(),'worker':worker,'result':result})
                print(json.dumps({'event':'local_TG_cycle','role':role,'worker':worker,'cycle':cycle,'status':result.get('status',result.get('activity'))}),flush=True)
            except Exception as exc:
                traceback.print_exc(limit=6)
                record_business_failure(root,role,exc,worker=worker if role=='fetch' else None)
                atomic_json(root/(role+'-'+str(worker)+'-error.json'),{'at':time.time(),'error':type(exc).__name__,'message':str(exc)[:300]})
                print(json.dumps({'event':'local_TG_error','role':role,'worker':worker,'error':type(exc).__name__}),flush=True)
                raise SystemExit(1) from exc
            cycle+=1
            target=min(deadline,started+(30 if not (root/'initialized.json').exists() else period))
            while time.monotonic()<target:
                time.sleep(min(30,max(0,target-time.monotonic())))
                print(json.dumps({'event':'local_TG_heartbeat','role':role,'worker':worker,'cycle':cycle}),flush=True)


def publish_once(root):
    from .pipeline import publish_latest
    root=Path(root)
    with lock(root,'publish'):
        if not (root/'initialized.json').exists():return {'status':'waiting_for_migration'}
        try:
            result=publish_latest(root/'clean')
            receipt=root/'clean/audits/publication-latest.json'
            if receipt.exists():result['publication']=json.loads(receipt.read_text())
            has_new=result.get('status')=='verified' or int((result.get('publication') or {}).get('D1_rows_upserted') or 0)>0
            record_business_success(root,'publish',has_new_items=has_new,summary={'status':result.get('status')})
            atomic_json(root/'publication-latest.json',{'at':time.time(),**result})
            return result
        except Exception as exc:
            record_business_failure(root,'publish',exc)
            raise


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('role',choices=['initialize','fetch','clean','publish','migrate-v2'])
    parser.add_argument('--state-dir',required=True)
    parser.add_argument('--source-dir')
    parser.add_argument('--worker',type=int,choices=range(WORKERS),default=0)
    parser.add_argument('--duration',type=int,default=6000)
    args=parser.parse_args()
    os.environ['FMR_TG_LOCAL_RUNTIME']='true'
    root=Path(args.state_dir).resolve()
    if args.role=='initialize':initialize(root,args.source_dir)
    elif args.role=='migrate-v2':
        from .evidence_store import migrate_existing_state_to_v2
        print(json.dumps(migrate_existing_state_to_v2(root)),flush=True)
    elif args.role=='publish':print(json.dumps(publish_once(root)),flush=True)
    else:loop(root,args.role,args.worker,args.duration)


if __name__=='__main__':main()
