"""Single ten-minute D1 writer. Collectors and their caches stay off D1."""
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import time
from fmr_core.d1_client import D1Client
from .catalogue_store import Store,digest
from .catalogue_sync import publish_device,publish_release,FirmwareIdentityPending
from .d1_budget import BudgetDB,D1BudgetReached
from .staged_catalogue import Artifacts,ROOT,encrypt_file,open_local
from .vendor_catalogue import VENDORS,SUPPRESSED_VENDORS

MAX_RECORDS=int(os.getenv('FMR_CATALOGUE_PUBLISH_MAX_RECORDS','180'))
if MAX_RECORDS!=0 and not 1<=MAX_RECORDS<=5000:raise ValueError('Invalid publication record budget')
SHARD_INDEX=int(os.getenv('FMR_CATALOGUE_SHARD_INDEX','0'))
SHARD_COUNT=int(os.getenv('FMR_CATALOGUE_SHARD_COUNT','1'))
if not 0<=SHARD_INDEX<SHARD_COUNT:raise ValueError('Invalid publication shard')


def open_ledger(path):
    db=D1Client(local_sqlite_path=str(path))
    db.execute('CREATE TABLE IF NOT EXISTS published(vendor TEXT,key TEXT,hash TEXT,PRIMARY KEY(vendor,key))')
    db.execute('CREATE TABLE IF NOT EXISTS control(key TEXT PRIMARY KEY,value TEXT)')
    return db


def candidates(local,ledger,vendor):
    store=Store(local,vendor)
    position=0
    for stage in ('release','model'):
        for key,value in store.rows(stage):
            if value.get('status')=='excluded' or value['device'].get('catalogue_scope')=='non_android':continue
            if stage=='release' and (value.get('status')!='resolved' or (value.get('published') and not value.get('metadata_dirty'))):continue
            from .release_metadata import NORMALIZATION_REVISION as REVISION
            body=value['device'] if stage=='model' else [value['device'],value['release'],REVISION]
            fingerprint=digest(body)
            old=ledger.query('SELECT hash FROM published WHERE vendor=? AND key=?',(vendor,key))
            if old and old[0]['hash']==fingerprint:continue
            # Split the actual pending work, not the whole snapshot: a skewed
            # backlog leaves most hash buckets empty.
            if SHARD_COUNT>1 and position%SHARD_COUNT!=SHARD_INDEX:
                position+=1
                continue
            position+=1
            yield stage,key,value,fingerprint


def sync_one(db,ledger,vendor,candidate):
    stage,key,value,fingerprint=candidate
    if stage=='model':publish_device(db,value['device']);result={'status':'published_new'}
    else:
        try:result=publish_release(db,value['device'],value['release'])
        except FirmwareIdentityPending:
            print('Firmware model identity conflict; retained pending, no acknowledgement.',flush=True);return False
    if result['status'] in ('published_new','preserved_existing'):
        # Commit local acknowledgement only after the D1 transaction succeeded.
        ledger.execute('INSERT INTO published(vendor,key,hash) VALUES (?,?,?) ON CONFLICT(vendor,key) DO UPDATE SET hash=excluded.hash',(vendor,key,fingerprint))
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
    completed=failed=0
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
            try:work[vendor]=iter(list(candidates(local,ledger,vendor)))
            finally:local.close()
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
            # Existing production brands are already bootstrapped. Never rewrite
            # brand names/anchor and cache counters on every sync.
            attempted=0;consecutive_errors=0
            while active and (MAX_RECORDS==0 or attempted<MAX_RECORDS):
                for vendor in list(active):
                    item=next(active[vendor],None)
                    if item is None:del active[vendor];continue
                    attempted+=1
                    try:
                        if sync_one(db,ledger,vendor,item):completed+=1
                        else:failed+=1
                        consecutive_errors=0
                    except D1BudgetReached:raise
                    except Exception as exc:
                        # Safe aggregate errors, no URLs or package identifiers.
                        print(vendor+': '+type(exc).__name__+'；留待下次同步。',flush=True);failed+=1
                        consecutive_errors+=1
                        if consecutive_errors>=3:raise RuntimeError('PublicationCircuitOpen') from exc
                    if attempted%20==0:print(f'已同步 {completed} 条；失败待重试 {failed}；D1 读 {db.rows_read} 行 / 写 {db.rows_written} 行。',flush=True)
                    if MAX_RECORDS and attempted>=MAX_RECORDS:break
        ledger.execute("INSERT INTO control(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(slot_key,str(slot)))
        summary=f'本次新增/确认 {completed} 条，失败待重试 {failed} 条。D1 读 {db.rows_read if db else 0} 行，写 {db.rows_written if db else 0} 行。采集检查点/页面缓存不写 D1。'
        print(summary,flush=True)
        if os.environ.get('GITHUB_STEP_SUMMARY'):Path(os.environ['GITHUB_STEP_SUMMARY']).write_text('# 统一 D1 增量同步\n\n'+summary+'\n\n每 10 分钟计划执行；GitHub 排队可能延后。未完成队列保留，下一批继续。\n')
        return 0
    except D1BudgetReached:
        if db and getattr(db,'provider_quota',False) and ledger:
            ledger.execute("INSERT INTO control(key,value) VALUES ('provider_quota_day',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(datetime.now(timezone.utc).strftime('%Y-%m-%d'),))
        print('本次 D1 行预算已到；已成功记录保留，剩余下次继续。',flush=True);return 0
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
            finally:artifacts.close()


if __name__=='__main__':raise SystemExit(main())
