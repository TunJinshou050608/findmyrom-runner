"""Single ten-minute D1 writer. Collectors and their caches stay off D1."""
from contextlib import ExitStack
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import time
from fmr_core.d1_client import D1Client
from .catalogue_store import Store,digest
from .catalogue_sync import publish_device,publish_release,publish_unavailable,FirmwareIdentityPending
from .d1_budget import BudgetDB,D1BudgetReached
from .staged_catalogue import Artifacts,ROOT,encrypt_file,open_local
from .vendor_catalogue import VENDORS,SUPPRESSED_VENDORS

MAX_RECORDS=int(os.getenv('FMR_CATALOGUE_PUBLISH_MAX_RECORDS','180'))
if MAX_RECORDS!=0 and not 1<=MAX_RECORDS<=5000:raise ValueError('Invalid publication record budget')
SHARD_INDEX=int(os.getenv('FMR_CATALOGUE_SHARD_INDEX','0'))
SHARD_COUNT=int(os.getenv('FMR_CATALOGUE_SHARD_COUNT','1'))
if not 0<=SHARD_INDEX<SHARD_COUNT:raise ValueError('Invalid publication shard')

# Keep the legacy table name so existing encrypted checkpoints remain reusable.
# Keys already contain the vendor; the same review queue serves every publisher.
IDENTITY_REVIEW_DELAY = 86400
IDENTITY_REVIEW_SQL = '''INSERT INTO xiaomi_publication_review(key,hash,retry_at) VALUES (?,?,?)
    ON CONFLICT(key) DO UPDATE SET hash=excluded.hash,retry_at=excluded.retry_at'''
ACK_SQL = '''INSERT INTO published(vendor,key,hash) VALUES (?,?,?)
    ON CONFLICT(vendor,key) DO UPDATE SET hash=excluded.hash'''
ACK_STAMP_SQL = '''INSERT INTO publication_ack_stamp(vendor,key,hash,confirmed_at) VALUES (?,?,?,?)
    ON CONFLICT(vendor,key) DO UPDATE SET hash=excluded.hash,confirmed_at=excluded.confirmed_at'''


def open_ledger(path):
    db=D1Client(local_sqlite_path=str(path))
    db.execute('CREATE TABLE IF NOT EXISTS published(vendor TEXT,key TEXT,hash TEXT,PRIMARY KEY(vendor,key))')
    db.execute('CREATE TABLE IF NOT EXISTS control(key TEXT PRIMARY KEY,value TEXT)')
    db.execute('CREATE TABLE IF NOT EXISTS xiaomi_publication_review(key TEXT PRIMARY KEY,hash TEXT NOT NULL,retry_at INTEGER NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS publication_ack_stamp(vendor TEXT,key TEXT,hash TEXT NOT NULL,confirmed_at INTEGER NOT NULL,PRIMARY KEY(vendor,key))')
    return db


def acknowledgement(vendor,key,fingerprint):
    return [(ACK_SQL,(vendor,key,fingerprint)),
            (ACK_STAMP_SQL,(vendor,key,fingerprint,time.time_ns()))]


def candidates(local,ledger,vendor,stats=None,*,respect_review=True):
    store=Store(local,vendor)
    review = {}
    if respect_review:
        review = {row['key']:row['hash'] for row in ledger.query(
            'SELECT key,hash FROM xiaomi_publication_review WHERE key GLOB ? AND retry_at>?',
            ('v2:'+vendor+':release:*',int(time.time())))}
    acknowledged_changes=[]
    def flush_acknowledgements():
        if acknowledged_changes:
            local.execute_batch([
                ("DELETE FROM publication_changes WHERE key=? AND revision=?", item)
                for item in acknowledged_changes])
            acknowledged_changes.clear()
    def acknowledge(key,revision):
        acknowledged_changes.append((key,revision))
        if len(acknowledged_changes)>=128:
            flush_acknowledgements()
    try:
        position=0
        for stage in ('release','model'):
            for key,value,change_revision in store.publication_rows(stage):
                if value.get('status')=='excluded' or value['device'].get('catalogue_scope')=='non_android':
                    acknowledge(key,change_revision)
                    continue
                unavailable=stage=='release' and vendor=='xiaomi' and value.get('status')=='unavailable'
                if stage=='release' and not unavailable and (value.get('status')!='resolved' or (value.get('published') and not value.get('metadata_dirty'))):
                    acknowledge(key,change_revision)
                    continue
                from .release_metadata import NORMALIZATION_REVISION as REVISION
                if stage == 'model':
                    body = value['device']
                else:
                    # The publisher recalculates public metadata from the source
                    # fields. A rewritten collector cache does not change D1 data.
                    release = {k: v for k, v in value['release'].items() if k != '_metadata'}
                    body = [value['device'], release, REVISION]
                if unavailable:body=[body,'unavailable',value.get('error')]
                fingerprint=digest(body)
                saved=ledger.query('SELECT hash FROM published WHERE vendor=? AND key=?',(vendor,key))
                acknowledged=saved[0]['hash'] if saved else None
                if acknowledged:
                    if acknowledged == fingerprint:
                        acknowledge(key,change_revision)
                        continue
                    # Revision 3 changed the fingerprint for every vendor. Keep
                    # those acknowledgements only for the identical release body.
                    if stage == 'release':
                        legacy = [value['device'], value['release'], REVISION]
                        if unavailable:
                            legacy = [legacy, 'unavailable', value.get('error')]
                        if acknowledged == digest(legacy):
                            acknowledge(key,change_revision)
                            continue
                        if not unavailable and acknowledged == digest([value['device'], value['release'], 3]):
                            acknowledge(key,change_revision)
                            continue
                if stage == 'release' and review.get(key) == fingerprint:
                    if stats is not None:
                        stats['identity_review_deferred'] += 1
                    continue
                # Split the actual pending work, not the whole snapshot: a skewed
                # backlog leaves most hash buckets empty.
                if SHARD_COUNT>1 and position%SHARD_COUNT!=SHARD_INDEX:
                    position+=1
                    continue
                position+=1
                yield stage,key,value,fingerprint
    finally:
        flush_acknowledgements()


def sync_one(db,ledger,vendor,candidate,outcomes=None):
    stage,key,value,fingerprint=candidate
    if stage=='model':publish_device(db,value['device']);result={'status':'published_new'}
    else:
        try:
            result=publish_unavailable(db,value['device'],value['release'],value.get('error')) if vendor=='xiaomi' and value.get('status')=='unavailable' else publish_release(db,value['device'],value['release'])
        except FirmwareIdentityPending as error:
            ledger.execute(IDENTITY_REVIEW_SQL,(key,fingerprint,int(time.time())+IDENTITY_REVIEW_DELAY))
            if outcomes is not None:
                outcomes['identity_pending'] += 1
            print(json.dumps({'event':'publication_identity_pending','vendor':vendor,'key':key,'reason':str(error)}),flush=True)
            return False
    if result['status'] in ('published_new','preserved_existing','unavailable_recorded'):
        if stage == 'release' and 0 < MAX_RECORDS <= 20 and result['status'] == 'preserved_existing' and not result.get('metadata_changed'):
            previous = ledger.query('SELECT 1 FROM published WHERE vendor=? AND key=?', (vendor,key))
            print(json.dumps({'event':'publication_existing_sample','vendor':vendor,'key':key,
                              'local_ack':'fingerprint_changed' if previous else 'missing'}),flush=True)
        # Commit local acknowledgement only after the D1 transaction succeeded.
        ledger.execute_batch(acknowledgement(vendor,key,fingerprint))
        if outcomes is not None:
            if stage == 'model':
                outcomes['devices_confirmed'] += 1
            elif result['status'] == 'published_new':
                outcomes['new_packages'] += 1
            elif result['status'] == 'unavailable_recorded':
                outcomes['unavailable_recorded'] += 1
            elif result.get('metadata_changed'):
                outcomes['metadata_updated'] += 1
            else:
                outcomes['existing_confirmed'] += 1
        return True
    return False


def main():
    if os.environ.get('FMR_D1_WRITES_PAUSED')=='true':
        print('D1 发布已暂停；本次不连接 D1，不读取远端快照，不改动确认点。',flush=True)
        return 0
    requested=os.environ.get('FMR_CATALOGUE_PUBLISH_VENDORS','').strip()
    selected=tuple(v.strip() for v in requested.split(',') if v.strip()) if requested else VENDORS
    if not selected or len(set(selected))!=len(selected) or any(v not in VENDORS for v in selected):raise ValueError('Invalid publication vendor selection')
    slot_key='slot:'+','.join(sorted(selected)) if requested else 'slot'
    ROOT.mkdir(parents=True,exist_ok=True,mode=0o700);path=ROOT/'publisher.sqlite';artifacts=Artifacts();ledger=None;db=None
    from collections import Counter
    outcomes=Counter()
    completed=failed=errors=0
    snapshots=ExitStack()
    try:
        artifacts.restore('publisher',path,baseline=False)
        ledger=open_ledger(path)
        quota=ledger.query("SELECT value FROM control WHERE key='provider_quota_day'")
        if quota and quota[0]['value']==datetime.now(timezone.utc).strftime('%Y-%m-%d'):
            print('D1 provider quota exhausted today; local queue retained, zero remote requests.',flush=True);return 0
        slot=int(time.time())//600
        old=ledger.query("SELECT value FROM control WHERE key=?",(slot_key,))
        if old and int(old[0]['value'])>=slot and os.getenv('FMR_CATALOGUE_PUBLISH_FORCE')!='true':
            print('本 10 分钟窗口已同步，跳过重复触发；D1 请求为 0。',flush=True);return 0
        # Round-robin prevents a large Xiaomi backlog starving every other vendor.
        # Sharded runs need a deterministic vendor order so every process sees
        # the same pending-work sequence.
        offset=slot%len(selected);vendors=selected if SHARD_COUNT>1 else selected[offset:]+selected[:offset]
        work={}
        for vendor in vendors:
            if vendor in SUPPRESSED_VENDORS:continue
            print(vendor+': 正在接续最新采集快照。',flush=True)
            local_path=ROOT/(vendor+'.sqlite')
            if not artifacts.restore(vendor,local_path):continue
            local=open_local(local_path,initialize=False)
            snapshots.callback(local.close)
            work[vendor]=candidates(local,ledger,vendor,outcomes)
            snapshots.callback(work[vendor].close)
        # Peek without losing the first record.
        import itertools
        active={}
        for vendor,items in work.items():
            first=next(items,None)
            if first is not None:active[vendor]=itertools.chain([first],items)
        if not active:
            print('没有新增或变化记录；D1 请求为 0。',flush=True)
        else:
            db=BudgetDB(cf_account_id=os.environ['CF_ACCOUNT_ID'],cf_api_token=os.environ['CF_API_TOKEN'],cf_database_id=os.environ['CF_D1_DATABASE_ID'],enforce_limits=os.getenv('FMR_D1_BUDGET_ENFORCE','true')!='false')
            if not db.acquire():
                print('今日发布预算已满；采集继续，未发布队列保留，下一 UTC 日恢复。',flush=True);return 0
            from fmr_core.schema_migrations import ensure_schema
            ensure_schema(db)
            sample_costs = 0 < MAX_RECORDS <= 20
            if sample_costs:
                print(json.dumps({'event':'publication_schema_cost','rows_read':db.rows_read,'rows_written':db.rows_written}),flush=True)
            db._publication_device_cache = {}
            # Existing production brands are already bootstrapped. Never rewrite
            # brand names/anchor and cache counters on every sync.
            attempted=0;consecutive_errors=0
            while active and (MAX_RECORDS==0 or attempted<MAX_RECORDS):
                for vendor in list(active):
                    item=next(active[vendor],None)
                    if item is None:
                        del active[vendor]
                        continue
                    attempted+=1
                    if sample_costs:
                        before_reads,before_writes=db.rows_read,db.rows_written
                        before_outcomes=outcomes.copy()
                    try:
                        if sync_one(db,ledger,vendor,item,outcomes):completed+=1
                        else:
                            failed+=1
                        consecutive_errors=0
                    except D1BudgetReached:raise
                    except Exception as exc:
                        errors+=1
                        # Safe aggregate errors, no URLs or package identifiers.
                        print(vendor+': '+type(exc).__name__+'；留待下次同步。',flush=True);failed+=1
                        consecutive_errors+=1
                        if consecutive_errors>=3:raise RuntimeError('PublicationCircuitOpen') from exc
                    if sample_costs:
                        outcome=next(iter(outcomes-before_outcomes),'unconfirmed')
                        print(json.dumps({'event':'publication_sample_cost','outcome':outcome,
                                          'rows_read':db.rows_read-before_reads,'rows_written':db.rows_written-before_writes}),flush=True)
                    if attempted%20==0:print(f'已同步 {completed} 条；失败待重试 {failed}；D1 读 {db.rows_read} 行 / 写 {db.rows_written} 行。',flush=True)
                    if MAX_RECORDS and attempted>=MAX_RECORDS:break
        ledger.execute("INSERT INTO control(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(slot_key,str(slot)))
        summary=f'本次新增/确认 {completed} 条，失败待重试 {failed} 条。D1 读 {db.rows_read if db else 0} 行，写 {db.rows_written if db else 0} 行。采集检查点/页面缓存不写 D1。'
        print(summary,flush=True)
        print(json.dumps({'event':'publication_outcomes',**outcomes}),flush=True)
        if os.environ.get('GITHUB_STEP_SUMMARY'):Path(os.environ['GITHUB_STEP_SUMMARY']).write_text('# 统一 D1 增量同步\n\n'+summary+'\n\n每 10 分钟计划执行；GitHub 排队可能延后。未完成队列保留，下一批继续。\n')
        return int(errors>0)
    except D1BudgetReached:
        if db and getattr(db,'provider_quota',False) and ledger:
            ledger.execute("INSERT INTO control(key,value) VALUES ('provider_quota_day',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(datetime.now(timezone.utc).strftime('%Y-%m-%d'),))
        print(json.dumps({'event':'publication_outcomes','budget_stop':True,**outcomes}),flush=True)
        print('本次 D1 行预算已到；已成功记录保留，剩余下次继续。',flush=True);return int(errors>0)
    finally:
        # Successful D1 writes must retain their local acknowledgements even if
        # budget settlement fails. Otherwise the next runner replays them.
        try:
            if db:
                try:db.settle()
                finally:db.close()
        finally:
            try:
                if ledger:
                    try:ledger.close()
                    finally:
                        if os.getenv('FMR_CATALOGUE_SKIP_EXPORT')!='true':
                            encrypt_file('publisher',path)
            finally:
                try:snapshots.close()
                finally:artifacts.close()


if __name__=='__main__':raise SystemExit(main())
