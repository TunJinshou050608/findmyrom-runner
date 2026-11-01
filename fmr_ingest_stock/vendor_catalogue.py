"""One vendor per workflow; durable, incremental metadata collection. No ROM bodies."""
import argparse
from datetime import datetime,timezone,timedelta
import json
import os
from pathlib import Path
import re
import threading
import time
from urllib.parse import parse_qs,urlsplit
import httpx

from fmr_core.d1_client import D1Client
from .device_sync import ensure_base_brands_and_anchor
from .catalogue_store import Store,digest
from .catalogue_cache import CachedClient,CatalogueDeadline
from .d1_budget import BudgetDB,D1BudgetReached,NOT_BEFORE
from .catalogue_job import collect_model
from .catalogue_sync import publish_device,publish_release
from .oem_sources import (MetadataHTTP,ATLAS_BRANDS,RESTRICTED,atlas_devices,
    google_catalogue,xiaomi_devices,product_devices,resolve_atlas,download_origin)

LABELS={'sony':'Sony Xperia','xiaomi':'小米 / Redmi / POCO','google':'Google Pixel / Nexus','oneplus':'一加',
    'samsung':'三星','motorola':'摩托罗拉','oppo':'OPPO','realme':'realme','vivo':'vivo',
    'asus':'ASUS','nothing':'Nothing','huawei':'华为','honor':'荣耀'}
VENDORS=tuple(LABELS)
MODEL_TTL=6*3600
RELEASE_TTL=30*86400
FAILURE_TTL=86400
ADAPTER_REVISION='20261101-download-refresh-1'


def upgrade_adapters(store,status):
    """Retry affected local records once per adapter revision, never per start."""
    if store.vendor not in ('oneplus','oppo','realme','vivo','asus'):return
    revision=ADAPTER_REVISION+('-asus-2' if store.vendor=='asus' else '')
    key=store.key('adapters')
    if (store.get(key) or {}).get('revision')==revision:return
    from .download_refresh import expiring
    changes=[];excluded=set();android=set()
    for target,value in store.rows('release'):
        row=value['release'];device=value['device']['upstream_id']
        if store.vendor=='asus' and row.get('kind')=='chromeos':
            excluded.add(device)
            value.update(status='excluded',error=None,next_check=2**40)
            changes.append((target,value));continue
        android.add(device)
        if value.get('status') in ('blocked','failed','retry') or expiring(row.get('download_url','')):
            value.update(status='pending',next_check=0,last_cycle='',attempts=0,published=False,error=None)
            changes.append((target,value))
    for target,value in store.rows('model'):
        if value['device']['upstream_id'] in excluded-android:
            value['device']['catalogue_scope']='non_android'
            value.update(status='excluded',error=None,next_check=2**40)
            changes.append((target,value))
    store.put_many(changes)
    store.put(key,{'revision':revision})
    status.log(f'下载适配器已更新；定向重新核对 {len(changes)} 条本机记录，已成功的稳定链接保持不变。')


def utc8(epoch=None):
    return datetime.fromtimestamp(time.time() if epoch is None else epoch,timezone(timedelta(hours=8))).strftime('%m-%d %H:%M:%S')


class Status:
    def __init__(self,vendor,path=None,interval=30):
        self.vendor=vendor;self.path=path or os.environ.get('GITHUB_STEP_SUMMARY')
        self.interval=interval;self.stop=threading.Event();self.lock=threading.RLock()
        self.started=time.time();self.phase='准备连接私有检查点';self.changed=self.started
        self.checkpoint=self.started;self.counts={};self.stats={};self.batch=1
        self.thread=None

    def log(self,message):
        print(f'[{utc8()}] [{LABELS[self.vendor]}] {message}',flush=True)

    def update(self,phase=None,counts=None,stats=None,checkpoint=False):
        with self.lock:
            if phase is not None:self.phase=phase;self.changed=time.time()
            if counts is not None:self.counts=counts
            if stats is not None:self.stats=stats
            if checkpoint:self.checkpoint=time.time()
            self.render()

    def render(self):
        if not self.path:return
        lines=[f'# {LABELS[self.vendor]}：固件目录更新',
            f'\n当前阶段：**{self.phase}** · 第 {self.batch} 批',
            f'\n最近检查点：北京时间 {utc8(self.checkpoint)}',
            '\n本工作流处理目录和下载链接；没有执行真实镜像下载、提取或发布验收。',
            '\n| 对象 | 状态 | 数量 |','| --- | --- | ---: |']
        names={'models':'机型','releases':'固件记录'}
        states={'pending':'待处理','ok':'已检查','resolved':'链接已解析','blocked':'来源受限 / 待解决','failed':'重试耗尽','retry':'等待重试'}
        for stage,values in self.counts.items():
            for state,n in values.items():lines.append(f'| {names.get(stage,stage)} | {states.get(state,state)} | {n} |')
        lines+=['\n## 本批页面请求',f"\n网络请求 {self.stats.get('network',0)} · 缓存复用 {self.stats.get('cache_hits',0)} · 304 未变化 {self.stats.get('not_modified',0)}",
            '\n中断后重跑本厂商入口即可续跑。已完成数据保留；默认增量，不清空队列。',
            '\n实时心跳在采集步骤日志中；GitHub 通常在步骤结束后展示这里的汇总。']
        Path(self.path).write_text('\n'.join(lines)+'\n')

    def beat(self):
        while not self.stop.wait(self.interval):
            with self.lock:
                elapsed=int(time.time()-self.changed);age=int(time.time()-self.checkpoint)
                self.log(f"仍在执行：{self.phase}；本阶段 {elapsed} 秒，距最近检查点 {age} 秒；网络 {self.stats.get('network',0)} 次，缓存复用 {self.stats.get('cache_hits',0)} 次。")
                self.render()

    def __enter__(self):
        self.thread=threading.Thread(target=self.beat,daemon=True);self.thread.start()
        self.update();self.log('开始运行；将复用私有检查点，每 30 秒报告执行状态。')
        return self

    def __exit__(self,*args):
        self.stop.set();self.thread.join(timeout=1)


def identity(device,row=None):
    if row is None:return device['upstream_id']
    return [device['upstream_id'],row.get('version'),row.get('region'),row.get('kind')]


def input_hash(row):
    # Resolver enrichment (filename/checksum/OEM redirect) is not a new source entry.
    keys=['version','region','kind','detail_page']
    if not row.get('detail_page'):keys+=['download_url','sha256','md5']
    return digest({k:row.get(k) for k in keys})


def durable_download(vendor,row):
    from .download_refresh import expiring, valid_release
    url=row.get('download_url')
    if row.get('refresh_source'):
        return valid_release(vendor,row)
    if vendor=='sony' and row.get('kind')=='sony_bundle':
        from .sony_source import validate_manifest,manifest_digest
        validate_manifest(row['bundle'])
        return url=='https://findmyrom.com/sony/bundles/'+manifest_digest(row['bundle'])
    if not url or not download_origin(vendor,url):return False
    return not expiring(url)


def model_value(device,embedded,old):
    if old:
        changed=embedded is not None and digest(embedded)!=digest(old.get('embedded'))
        old.update(device=device)
        if embedded is not None:old['embedded']=embedded
        if changed:old.update(next_check=0,last_cycle='')
        return old
    return {'device':device,'embedded':embedded,'status':'pending','next_check':0,'attempts':0}


def seed_models(store,devices,embedded):
    for offset in range(0,len(devices),10):
        chunk=devices[offset:offset+10]
        keys=[store.key('model',identity(d)) for d in chunk]
        old=store.get_many(keys)
        store.put_many([(key,model_value(d,embedded.get(d['upstream_id']),old.get(key))) for key,d in zip(keys,chunk)])


def seed_releases(store,device,rows):
    new=0
    # Lookup and checkpoint in batches, rather than two D1 round trips per version.
    for offset in range(0,len(rows),50):
        chunk=rows[offset:offset+50]
        keys=[store.key('release',identity(device,row)) for row in chunk]
        old=store.get_many(keys);writes={}
        for key,row in zip(keys,chunk):
            signature=input_hash(row)
            if key in old and old[key].get('input_hash')==signature:continue
            writes[key]={'device':device,'release':row,'input_hash':signature,'status':'pending','next_check':0,
                'attempts':0,'published':False,'source_changed':key in old}
        store.put_many(list(writes.items()));new+=len(writes)
    return new


def migrate(store,status,limit=500):
    """Import legacy work once, without HTTP refetch or touching the old rows."""
    key=store.key('migration');state=store.get(key) or {'cursor':'','done':False,'imported':0}
    if state['done']:return True
    remaining=limit
    status.update('接续旧任务的检查点')
    while remaining:
        take=min(25,remaining)
        rows=store.db.query('''SELECT source_target,last_cursor_val,last_checked_at FROM crawler_sources
            WHERE device_codename='__global__' AND source_kind='oem_catalogue'
            AND source_target>? AND json_extract(last_cursor_val,'$.device.vendor')=?
            ORDER BY source_target LIMIT ?''',(state['cursor'],store.vendor,take))
        if not rows:
            state['done']=True;store.put(key,state)
            status.log(f"旧任务接续完成，复用了 {state['imported']} 个检查点。")
            return True
        destinations=[store.key('release' if 'release' in json.loads(r['last_cursor_val']) else 'model', identity(json.loads(r['last_cursor_val'])['device'],json.loads(r['last_cursor_val']).get('release'))) for r in rows]
        existing=store.get_many(destinations)
        writes=[]
        for record in rows:
            value=json.loads(record['last_cursor_val']);device=value['device'];checked=record['last_checked_at']
            stage='release' if 'release' in value else 'model'
            dest=store.key(stage,identity(device,value.get('release')))
            old=existing.get(dest)
            if old and (old.get('checked_at',0)>checked or old.get('status') in ('ok','resolved')):continue
            if stage=='release':
                row=value['release'];resolved=durable_download(store.vendor,row)
                value.update(input_hash=input_hash(row),status='resolved' if resolved else 'blocked' if value.get('done') else 'pending',
                    next_check=checked+(RELEASE_TTL if resolved else FAILURE_TTL) if value.get('done') else 0,
                    published=value.get('publication',{}).get('status') in ('published_new','preserved_existing'))
            else:
                provenance=value.get('provenance') or {}
                good=value.get('done') and not value.get('error') and not any(provenance.get(k) for k in ('errors','conflicts','fota_errors','blocker'))
                value.update(status='ok' if good else 'pending',next_check=checked+MODEL_TTL if good else 0)
            value.update(checked_at=checked,attempts=0,migrated=True)
            writes.append((dest,value));state['imported']+=1
        state['cursor']=rows[-1]['source_target']
        store.put_many(writes+[(key,state)]);status.update(checkpoint=True)
        remaining-=len(rows)
        if len(rows)<take:
            state['done']=True;store.put(key,state);return True
    status.log(f"旧任务检查点已接续 {state['imported']} 条，下批接着迁移。")
    return False


def discover(store,control,http,status):
    now=int(time.time())
    if control.get('discovery_cycle')==control['cycle']:return
    if control.get('discovery_checked_at',0)+3600>now and control['operation']!='refresh_sources':
        status.log('机型列表仍在刷新周期内，复用已有列表。');return
    if not control.get('discovery_checked_at') and store.counts()['models']:
        control['discovery_checked_at']=now
        control['discovery_cycle']=control['cycle']
        status.log('沿用旧任务已经发现的机型列表，本次优先完成剩余任务。');return
    status.update('检查机型列表是否有更新');vendor=store.vendor
    embedded={};errors=[]
    if vendor=='sony':
        from .sony_source import devices as sony_devices
        devices=sony_devices(http)
    elif vendor=='google':devices,embedded,errors=google_catalogue(http)
    elif vendor=='xiaomi':devices,errors=xiaomi_devices(http)
    elif vendor in RESTRICTED:devices=product_devices(vendor,http)
    else:devices=atlas_devices(vendor,http)
    if errors:control['discovery_errors']=errors
    else:control.pop('discovery_errors',None)
    seed_models(store,devices,embedded)
    control['discovery_cycle']=control['cycle']
    control['discovery_checked_at']=now
    status.log(f'机型列表检查完成：发现 {len(devices)} 个机型；已有检查点已保留。')
    status.update(checkpoint=True)


def problem(exc):
    if isinstance(exc,httpx.HTTPStatusError):return 'HTTP_'+str(exc.response.status_code)
    return type(exc).__name__


def mark_problem(value,code,cycle,blocked=False):
    now=int(time.time())
    if value.get('attempt_cycle')!=cycle:value['attempts']=0
    value['attempt_cycle']=cycle;value['attempts']=value.get('attempts',0)+1
    value['error']=code;value['checked_at']=now
    if blocked or value['attempts']>=3:
        value.update(status='blocked' if blocked else 'failed',next_check=now+FAILURE_TTL,last_cycle=cycle)
    else:value.update(status='retry',next_check=0)


def batch(store,control,http,status):
    cycle=control['cycle'];now=int(time.time());changed=0
    if control['mode']=='publish':
        ready=store.ready()
        if ready:
            status.update(f'优先发布已经解析的 {len(ready)} 条记录')
            for number,(key,value) in enumerate(ready,1):
                result=publish_release(store.db,value['device'],value['release'])
                if result['status'] in ('published_new','preserved_existing'):
                    value.update(published=True,publication=result)
                else:mark_problem(value,'PublicationRejected',cycle,blocked=True)
                store.put(key,value);store.advance('publish',key);changed+=1
                status.update(checkpoint=True)
                if number%10==0:status.log(f'已发布并同步网页 {number}/{len(ready)} 条记录。')
            counts=store.counts();status.update(counts=counts,stats=http.client.stats)
            more=bool(store.ready_count or store.due('model',cycle,1,operation=control['operation']) or store.due('release',cycle,1,operation=control['operation'],publish=True))
            return more,counts,changed
    options={'operation':control['operation'],'publish':control['mode']=='publish'}
    models=store.due('model',cycle,4,now,**options)
    for number,(key,value) in enumerate(models,1):
        if time.monotonic()>=http.client.deadline:break
        status.update(f'检查本批第 {number}/{len(models)} 个机型的历史目录')
        try:
            device,rows,provenance=collect_model(value['device'],value.get('embedded'),http)
            if store.vendor=='asus' and rows and all(row.get('kind')=='chromeos' for row in rows):
                value.update(device={**device,'catalogue_scope':'non_android'},provenance=provenance,
                    releases=len(rows),checked_at=now,status='excluded',error=None,next_check=2**40,last_cycle=cycle)
                store.put(key,value);store.advance('model',key);changed+=1
                status.log('该目录为 ChromeOS 固件，已排除出 Android 官包采集。')
                continue
            new=0
            for offset in range(0,len(rows),100):
                if time.monotonic()>=http.client.deadline:raise CatalogueDeadline()
                new+=seed_releases(store,device,rows[offset:offset+100])
                status.log(f'本机型已核对 {min(offset+100,len(rows))}/{len(rows)} 条记录，新增或变化 {new} 条。');status.update(checkpoint=True)
            value.update(device=device,provenance=provenance,releases=len(rows),checked_at=now)
            issues=any(provenance.get(k) for k in ('errors','conflicts','fota_errors','source_errors','blocker')) or not rows
            if issues:
                mark_problem(value,'IncompleteSourceHistory',cycle,blocked=store.vendor in RESTRICTED)
                status.log(f"来源历史不完整，已记录为 {value['status']}，本轮第 {value['attempts']}/3 次检查。")
            else:value.update(status='ok',next_check=now+MODEL_TTL,last_cycle=cycle,attempts=0,error=None)
            if control['mode']=='publish':publish_device(store.db,device)
            status.log(f'机型目录已保存：{len(rows)} 条记录，新增或变化 {new} 条，其余沿用。')
        except D1BudgetReached:raise
        except CatalogueDeadline:
            status.log('本批时间预算已到；页面缓存和已保存记录保留，下批从检查点接着做。');break
        except Exception as exc:
            mark_problem(value,problem(exc),cycle)
            status.log(f"机型任务遇到 {value['error']}；状态：{value['status']}，本轮第 {value['attempts']}/3 次尝试。")
        store.put(key,value);store.advance('model',key);changed+=1;status.update(checkpoint=True)
    releases=store.due('release',cycle,100,now,**options)
    for number,(key,value) in enumerate(releases,1):
        if time.monotonic()>=http.client.deadline:break
        status.update(f'解析本批第 {number}/{len(releases)} 条下载记录')
        try:
            row=value['release']
            if value.get('status')!='resolved' or not durable_download(store.vendor,row) or value.get('checked_at',0)+RELEASE_TTL<=now:
                if row.get('detail_page'):row=resolve_atlas(row,http)
                if not durable_download(store.vendor,row):
                    mark_problem(value,'UnresolvedOrTemporaryDownload',cycle,blocked=True)
                    value['release']=row;store.put(key,value);changed+=1;status.update(checkpoint=True)
                    status.log('该下载记录暂无法取得可信稳定链接，已记下缺口并继续。');continue
            if control['mode']=='publish' and not value.get('published'):
                result=publish_release(store.db,value['device'],row)
                value['publication']=result
                if result['status'] not in ('published_new','preserved_existing'):
                    raise ValueError('PublicationRejected')
                value['published']=True
            value.update(release=row,status='resolved',checked_at=now,next_check=now+RELEASE_TTL,
                         last_cycle=cycle,attempts=0,error=None)
            store.put(key,value);store.advance('release',key);changed+=1;status.update(checkpoint=True)
        except D1BudgetReached:raise
        except CatalogueDeadline:
            status.log('本批时间预算已到；下载记录留在队列，下批继续。');break
        except Exception as exc:
            mark_problem(value,problem(exc),cycle);store.put(key,value);store.advance('release',key);changed+=1;status.update(checkpoint=True)
            status.log(f"下载记录遇到 {value['error']}；状态：{value['status']}，本轮第 {value['attempts']}/3 次尝试。")
        if number%10==0 or number==len(releases):
            status.log(f'本批已处理 {number}/{len(releases)} 条下载记录，检查点已保存。')
    counts=store.counts();status.update(counts=counts,stats=http.client.stats)
    more=bool(store.due('model',cycle,1,operation=control['operation']) or
              store.due('release',cycle,1,**options))
    return more,counts,changed


def dispatch(control,vendor):
    with httpx.Client(timeout=30,follow_redirects=False) as client:
        response=client.post('https://api.github.com/repos/'+os.environ['GITHUB_REPOSITORY']+
            '/actions/workflows/catalogue_'+vendor+'.yml/dispatches',
            headers={'Authorization':'Bearer '+os.environ['GH_WORKFLOW_TOKEN'],'Accept':'application/vnd.github+json'},
            json={'ref':control['ref'],'inputs':{'cycle':control['cycle'],'batch':str(control['batch']),
                  'mode':control['mode'],'operation':control['operation']}})
        if response.status_code not in (200,204):raise RuntimeError('ContinuationRejected')


def run(db,vendor,mode='audit',operation='incremental',cycle='',sequence=1,client=None,dispatch_next=True,time_budget=2700):
    store=Store(db,vendor);key=store.key('control');control=store.get(key)
    with Status(vendor) as status:
        upgrade_adapters(store,status)
        if int(os.environ.get('GITHUB_RUN_ATTEMPT','1'))>1 and control and control['state']=='completed_with_gaps':
            cycle='';mode=control['mode'];operation='retry_failed'
            status.log('收到失败任务的重新运行请求；复用成功记录，只重试失败项。')
        if not cycle and control and control['state'] in ('working','paused') and os.environ.get('GITHUB_EVENT_NAME')=='schedule':
            mode,operation=control['mode'],control['operation']
        if cycle:
            if not control or control['cycle']!=cycle or not (control['batch']==sequence or (control['state']=='paused' and control.get('continuation_pending') and control['batch']==sequence+1)) or control['state'] not in ('working','paused'):
                status.log('这是一条已过期或重复的续跑请求，当前进度保持不变。');return 0
        elif not control or control['state'] not in ('working','paused') or mode!=control['mode'] or operation!=control['operation']:
            previous=control or {}
            control={'schema_version':2,'cycle':'c'+os.environ['GITHUB_RUN_ID']+'a'+os.environ.get('GITHUB_RUN_ATTEMPT','1'), 'batch':1,
                'ref':os.environ['GITHUB_REF_NAME'],'mode':mode,'operation':operation,'state':'working',
                'discovery_checked_at':previous.get('discovery_checked_at',0)}
            store.put(key,control)
        else:
            # A fresh manual/scheduled job resumes the active queue instead of
            # resetting progress. Changing the explicit operation starts a new
            # cycle over the same persistent records; old continuations go stale.
            status.log('发现未结束的任务，直接从保存位置续接。')
        if control.get('schema_version')!=2:raise ValueError('Unsupported checkpoint version')
        initial_batch=control['batch']
        store.cursors=dict(control.get('cursors',{}))
        status.batch=control['batch'];status.update(counts=store.counts())
        status.log(f"第 {control['batch']} 批；运行方式 {control['mode']} / {control['operation']}。")
        cache=CachedClient(store,client,force=control['operation'] in ('refresh_sources','retry_failed'))
        cache.deadline=time.monotonic()+time_budget
        status.update(stats=cache.stats)
        http=MetadataHTTP(cache);http.history_workers=1
        try:
            if not migrate(store,status):
                more=True;counts=store.counts();changed=0
            else:
                discover(store,control,http,status)
                store.put(key,control)
                more,counts,changed=batch(store,control,http,status)
            control['cursors']=store.cursors
            control['last_code_sha']=os.environ.get('GITHUB_SHA');control['last_checkpoint']=int(time.time())
            control['batch']+=1
            gaps=bool(control.get('discovery_errors')) or vendor in RESTRICTED or any(
                values.get(s,0) for values in counts.values() for s in ('failed','blocked','retry'))
            control['state']='working' if more else 'completed_with_gaps' if gaps else 'completed'
            control['counts']=counts;control['firmware_extraction_verified']=False
            store.put(key,control)
            status.update('本批完成，自动接着处理下一批' if more else '本轮完成，仍有来源缺口' if gaps else '本轮增量检查完成',
                counts=counts,stats=cache.stats,checkpoint=True)
            status.log(f"本批保存 {changed} 个任务检查点；网络 {cache.stats['network']} 次，缓存复用 {cache.stats['cache_hits']} 次，304 未变化 {cache.stats['not_modified']} 次。")
            if more and dispatch_next:
                control['continuation_pending']=True;store.put(key,control)
                dispatch(control,vendor)
                control['continuation_pending']=False;store.put(key,control)
                status.log('下一批已交给本厂商工作流，结束本批。')
            elif more:status.log('本批检查点已保存到任务侧；上传加密快照后再续跑。D1 由统一同步任务处理。')
            else:status.log('本轮结束。下次运行仍使用已有检查点，只检查到期和新增内容。')
            return 0 if more or not gaps else 1
        except D1BudgetReached:
            control.update(state='working',batch=max(control['batch'],initial_batch+1),cursors=store.cursors)
            with db.checkpoint():store.put(key,control)
            status.update('本批 D1 预算已到，检查点已保留')
            status.log(f'本批 D1 读取 {db.rows_read} 行、写入 {db.rows_written} 行；下一批仍受每日总预算控制。')
            dispatch(control,vendor)
            return 0
        except CatalogueDeadline:
            control.update(state='working',batch=max(control['batch'],initial_batch+1),cursors=store.cursors)
            store.put(key,control)
            status.log('本批时间已到，已保存检查点；上传后由下一批续接。')
            if dispatch_next:dispatch(control,vendor)
            return 0
        except Exception as exc:
            control.update(state='paused',error=problem(exc),cursors=store.cursors);store.put(key,control)
            status.update('暂时中断；检查点已保留，重跑即可续接',stats=cache.stats)
            status.log(f"任务中断：{problem(exc)}。不会清空已经完成的数据。")
            return 1
        finally:cache.close()


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vendor',required=True,choices=VENDORS)
    parser.add_argument('--mode',choices=['audit','publish'],default='publish')
    parser.add_argument('--operation',choices=['incremental','retry_failed','refresh_sources'],default='incremental')
    parser.add_argument('--cycle',default='');parser.add_argument('--batch',type=int,default=1)
    args=parser.parse_args(argv)
    if os.environ.get('GITHUB_ACTIONS')!='true':raise ValueError('Requires a GitHub cloud runner')
    if args.cycle and not re.fullmatch(r'c[0-9]{1,22}(?:a[0-9]{1,6})?',args.cycle):raise ValueError('Invalid cycle')
    if not 1<=args.batch<=50000:raise ValueError('Invalid batch')
    if time.time()<NOT_BEFORE:
        print('D1 暂停至 2026-11-01 00:00 UTC；本次不连接数据库。',flush=True);return 0
    db=None
    print(f'[{utc8()}] [{LABELS[args.vendor]}] 正在连接私有数据库，读取已保存的进度。',flush=True)
    try:
        db=BudgetDB(cf_account_id=os.environ['CF_ACCOUNT_ID'],cf_api_token=os.environ['CF_API_TOKEN'],cf_database_id=os.environ['CF_D1_DATABASE_ID'])
        if not db.acquire():
            print('本项目今日 D1 预留预算已满，等待下个 UTC 日；已有网页数据与检查点保留。',flush=True);return 0
        ensure_base_brands_and_anchor(db)
        return run(db,args.vendor,args.mode,args.operation,args.cycle,args.batch)
    except Exception as exc:
        print(f'[{utc8()}] [{LABELS[args.vendor]}] 启动未完成：{problem(exc)}。请检查运行环境或数据库权限，已有数据保留。',flush=True)
        return 1
    finally:
        if db:db.close()


if __name__=='__main__':raise SystemExit(main())

