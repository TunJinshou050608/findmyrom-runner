"""Single ten-minute D1 writer. Collectors and their caches stay off D1."""
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import time
from fmr_core.d1_client import D1Client
from .catalogue_store import Store,digest
from .catalogue_sync import publish_device,publish_release
from .d1_budget import BudgetDB,D1BudgetReached
from .staged_catalogue import Artifacts,ROOT,encrypt_file,open_local
from .vendor_catalogue import VENDORS

MAX_RECORDS=180


def open_ledger(path):
    db=D1Client(local_sqlite_path=str(path))
    db.execute('CREATE TABLE IF NOT EXISTS published(vendor TEXT,key TEXT,hash TEXT,PRIMARY KEY(vendor,key))')
    db.execute('CREATE TABLE IF NOT EXISTS control(key TEXT PRIMARY KEY,value TEXT)')
    return db


def candidates(local,ledger,vendor):
    store=Store(local,vendor)
    for stage in ('release','model'):
        for key,value in store.rows(stage):
            if stage=='release' and (value.get('status')!='resolved' or value.get('published')):continue
            body=value['device'] if stage=='model' else [value['device'],value['release']]
            fingerprint=digest(body)
            old=ledger.query('SELECT hash FROM published WHERE vendor=? AND key=?',(vendor,key))
            if old and old[0]['hash']==fingerprint:continue
            yield stage,key,value,fingerprint


def sync_one(db,ledger,vendor,candidate):
    stage,key,value,fingerprint=candidate
    if stage=='model':publish_device(db,value['device']);result={'status':'published_new'}
    else:result=publish_release(db,value['device'],value['release'])
    if result['status'] in ('published_new','preserved_existing'):
        # Commit local acknowledgement only after the D1 transaction succeeded.
        ledger.execute('INSERT INTO published(vendor,key,hash) VALUES (?,?,?) ON CONFLICT(vendor,key) DO UPDATE SET hash=excluded.hash',(vendor,key,fingerprint))
        return True
    return False


def main():
    ROOT.mkdir(parents=True,exist_ok=True,mode=0o700);path=ROOT/'publisher.sqlite';artifacts=Artifacts();ledger=None;db=None
    completed=failed=0
    try:
        artifacts.restore('publisher',path,baseline=False)
        ledger=open_ledger(path)
        slot=int(time.time())//600
        old=ledger.query("SELECT value FROM control WHERE key='slot'")
        if old and int(old[0]['value'])>=slot:
            print('本 10 分钟窗口已同步，跳过重复触发；D1 请求为 0。',flush=True);return 0
        # Round-robin prevents a large Xiaomi backlog starving every other vendor.
        offset=slot%len(VENDORS);vendors=VENDORS[offset:]+VENDORS[:offset]
        work={}
        for vendor in vendors:
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
            db=BudgetDB(cf_account_id=os.environ['CF_ACCOUNT_ID'],cf_api_token=os.environ['CF_API_TOKEN'],cf_database_id=os.environ['CF_D1_DATABASE_ID'])
            if not db.acquire():
                print('今日发布预算已满；采集继续，未发布队列保留，下一 UTC 日恢复。',flush=True);return 0
            # Existing production brands are already bootstrapped. Never rewrite
            # brand names/anchor and cache counters on every sync.
            attempted=0;consecutive_errors=0
            while active and attempted<MAX_RECORDS:
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
                    if attempted>=MAX_RECORDS:break
        ledger.execute("INSERT INTO control(key,value) VALUES ('slot',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(str(slot),))
        summary=f'本次新增/确认 {completed} 条，失败待重试 {failed} 条。D1 读 {db.rows_read if db else 0} 行，写 {db.rows_written if db else 0} 行。采集检查点/页面缓存不写 D1。'
        print(summary,flush=True)
        if os.environ.get('GITHUB_STEP_SUMMARY'):Path(os.environ['GITHUB_STEP_SUMMARY']).write_text('# 统一 D1 增量同步\n\n'+summary+'\n\n每 10 分钟计划执行；GitHub 排队可能延后。未完成队列保留，下一批继续。\n')
        return 0
    except D1BudgetReached:
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
                    finally:encrypt_file('publisher',path)
            finally:artifacts.close()


if __name__=='__main__':raise SystemExit(main())
