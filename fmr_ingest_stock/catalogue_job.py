"""Autonomous all-OEM metadata catalogue jobs. No firmware bodies are requested.

Only a new experiment's private checkpoints are changed. Optional publication
is INSERT-ONLY, so running frozen firmware experiments retain their inputs.
"""
import argparse
from collections import Counter
import hashlib
import json
import os
import re
import time
import threading
from urllib.parse import urlsplit
from pathlib import Path
import httpx

from fmr_core.d1_client import D1Client
from .device_sync import ensure_base_brands_and_anchor
from .oem_sources import (MetadataHTTP,ATLAS_BRANDS,WEB_BRANDS,DEFERRED,RESTRICTED,
    atlas_devices,atlas_releases,resolve_atlas,google_catalogue,xiaomi_devices,product_devices,samsung_history)
from .history import discover_device
from .catalogue_sync import publish_device,publish_release

KIND='oem_catalogue'
ALL=tuple(ATLAS_BRANDS)+tuple(DEFERRED)


def progress(event, **counts):
    """Only aggregate operational data; never source URLs or firmware identities."""
    allowed = {'vendor','generation','records','processed','attempt','elapsed_seconds','phase'}
    if not set(counts) <= allowed:
        raise ValueError('Unsafe progress field')
    print(json.dumps({'event':event, **counts}, sort_keys=True), flush=True)


def heartbeat(stop):
    started = time.monotonic()
    while not stop.wait(30):
        progress('heartbeat', elapsed_seconds=int(time.monotonic()-started))


def save(db,key,value):
    data=json.dumps(value,sort_keys=True,separators=(',',':'))
    if len(data.encode())>1500000:raise ValueError('Checkpoint exceeds bound')
    db.execute('''INSERT INTO crawler_sources(device_codename,source_kind,source_target,last_cursor_val,last_checked_at)
        VALUES ('__global__',?,?,?,?) ON CONFLICT(device_codename,source_kind,source_target)
        DO UPDATE SET last_cursor_val=excluded.last_cursor_val,last_checked_at=excluded.last_checked_at''',
        (KIND,key,data,int(time.time())))


def get(db,key):
    rows=db.query("SELECT last_cursor_val FROM crawler_sources WHERE device_codename='__global__' AND source_kind=? AND source_target=?",(KIND,key))
    return json.loads(rows[0]['last_cursor_val']) if rows else None


def task_key(run,stage,identity):
    return run+':'+stage+':'+hashlib.sha256(identity.encode()).hexdigest()[:24]


def tasks(db,run,stage,limit=None):
    sql="SELECT source_target,last_cursor_val FROM crawler_sources WHERE device_codename='__global__' AND source_kind=? AND source_target GLOB ? AND json_extract(last_cursor_val,'$.done')=0 ORDER BY source_target"
    params=[KIND,run+':'+stage+':*']
    if limit:
        sql+=' LIMIT ?';params.append(limit)
    return [(r['source_target'],json.loads(r['last_cursor_val'])) for r in db.query(sql,params)]


def start(db,run,brands,mode,http):
    if not brands or any(b not in ALL for b in brands):raise ValueError('Unknown manufacturer')
    control={'run':run,'brands':brands,'mode':mode,'generation':0,'ref':os.environ['GITHUB_REF_NAME'],
             'sha':os.environ['GITHUB_SHA'],'state':'discovering','summary':{},'started_at':int(time.time())}
    save(db,run,control)
    for vendor in brands:
        progress('manufacturer_discovery_start',vendor=vendor)
        summary={'models':0,'release_records':0,'resolved_downloads':0,'published':0,
                 'errors':{},'extraction_verified':0,'source_scope':'observable_public_catalogues'}
        if vendor in DEFERRED:
            summary.update(status='deferred_by_user',reason=DEFERRED[vendor]);control['summary'][vendor]=summary;save(db,run,control);continue
        try:
            embedded={};errors=[]
            if vendor=='google':devices,embedded,errors=google_catalogue(http)
            elif vendor=='xiaomi':devices,errors=xiaomi_devices(http)
            elif vendor in RESTRICTED:devices=product_devices(vendor,http)
            else:devices=atlas_devices(vendor,http)
            for device in devices:
                key=task_key(run,'model',vendor+':'+device['upstream_id'])
                value={'device':device,'done':False,'embedded':embedded.get(device['upstream_id'])}
                save(db,key,value)
            summary.update(models=len(devices),status='models_discovered')
            if errors:summary['discovery_errors']=errors
            if vendor in RESTRICTED:summary['firmware_blocker']=RESTRICTED[vendor]
        except Exception as exc:summary.update(status='discovery_failed',errors={type(exc).__name__:1})
        control['summary'][vendor]=summary;save(db,run,control)
        progress('manufacturer_discovery_end',vendor=vendor,records=summary['models'])
    control['state']='working';save(db,run,control)
    return control


def bump(summary,error):
    summary['errors'][error]=summary['errors'].get(error,0)+1


def collect_model(device,embedded,http,history_previous=None):
    vendor=device['vendor'];provenance={}
    if vendor=='sony':
        from .sony_source import releases
        return releases(device,http)
    if vendor in RESTRICTED:return device,[],{'blocker':RESTRICTED[vendor]}
    if embedded is not None:return device,embedded,{'provider':'google_official'}
    if vendor=='xiaomi':
        from .history import HistoryCheckpoint
        store=getattr(http.client,'store',None)
        checkpoint=HistoryCheckpoint(store) if store and getattr(store.db,'mode',None)=='sqlite' else None
        rows,provenance=discover_device(device['upstream_id'],route_code=device.get('route_code'),
            client=http.client,max_workers=getattr(http,'history_workers',4),checkpoint=checkpoint,
            max_details=getattr(http,'history_detail_budget',32) if checkpoint else None,previous=history_previous)
        releases=[]
        from .firmware_sync import normalize
        for row in rows:
            n=normalize(row,'xiaomi')
            releases.append({'vendor':vendor,'device_id':device['upstream_id'],'version':row['version'],
                'region':n['region_code'],'android_version':n['android_version'],
                'kind':'full_ota' if n['package_type']=='recovery_zip' else 'fastboot',
                'date':n['published_date'],'size':n['file_size_str'],'md5':n['md5_or_sha256'],
                'download_url':n['original_official_url'],'origin_kind':'oem','resolution':'resolved',
                **({k:row[k] for k in ('variant_id','variant_label')} if row.get('variant_id') else {})})
        return device,releases,provenance
    device,rows=atlas_releases(device,http)
    if vendor=='samsung':
        from .samsung_fus import normalize_version
        observed=set()
        for r in rows:
            try:observed.add((normalize_version(r['version']).upper(),r['region']))
            except ValueError:pass
        regions=sorted({r['region'] for r in rows if re.fullmatch('[A-Z0-9]{3}',r['region'])})
        full=[];errors=[]
        for region in regions:
            try:
                for version in samsung_history(device['codename'],region,http):
                    if (normalize_version(version).upper(),region) in observed:continue
                    full.append({'vendor':vendor,'device_id':device['upstream_id'],'version':version,
                        'region':region,'kind':'samsung_firmware','android_version':'unknown',
                        'resolution':'requires_samsung_fus','source_reference':'https://fota-cloud-dn.ospserver.net/firmware/'+region+'/'+device['codename']+'/version.xml',
                        'origin_kind':'unresolved','metadata_source':'samsung_fota_version_xml'})
            except Exception as exc:
                if getattr(exc,'fmr_batch_deadline',False):raise
                errors.append({'region':region,'error':type(exc).__name__})
        rows.extend(full)
        from .samsung_metadata import attach
        attach(device,rows,http)
        provenance={'provider':'atlas_plus_samsung_fota','fota_errors':errors,
                                    'history_limits':'Manufacturer may omit withdrawn builds; encrypted/authenticated packages need a resolver.'}
    return device,rows,provenance


def step(db,control,http):
    run=control['run'];processed=0
    progress('batch_start',generation=control['generation'])
    # Model metadata fan-out is separate from release resolution; a large model
    # never forces all binary/download entries into one job's time window.
    for key,value in tasks(db,run,'model',8):
        device=value['device'];vendor=device['vendor'];summary=control['summary'][vendor]
        progress('task_start',vendor=vendor,attempt=value.get('attempts',0)+1,processed=processed)
        try:
            device,rows,provenance=collect_model(device,value.get('embedded'),http)
            if control['mode']=='publish':publish_device(db,device)
            for index,row in enumerate(rows):
                identity=vendor+':'+device['upstream_id']+':'+str(row.get('version'))+':'+str(row.get('region'))+':'+str(row.get('kind'))
                rkey=task_key(run,'release',identity)
                if get(db,rkey) is None:save(db,rkey,{'device':device,'release':row,'done':False})
                if (index+1)%50==0:progress('release_checkpoints_saved',vendor=vendor,records=index+1)
            summary['release_records']+=len(rows)
            value.update(device=device,provenance=provenance,releases=len(rows),done=True)
            if not rows and vendor not in RESTRICTED:bump(summary,'NoPublicReleases')
            if provenance.get('fota_errors'):bump(summary,'IncompleteSamsungHistory')
            if provenance.get('source_errors') or provenance.get('errors'):bump(summary,'IncompleteSourceHistory')
            if provenance.get('conflicts'):bump(summary,'SourceIdentityConflict')
        except Exception as exc:
            value['attempts']=value.get('attempts',0)+1
            value.update(done=value['attempts']>=3,error=type(exc).__name__)
            if value['done']:bump(summary,type(exc).__name__)
        save(db,key,value);processed+=1
        progress('task_checkpoint_saved',vendor=vendor,processed=processed)
    for key,value in tasks(db,run,'release',128):
        device=value['device'];vendor=device['vendor'];summary=control['summary'][vendor]
        progress('task_start',vendor=vendor,attempt=value.get('attempts',0)+1,processed=processed)
        try:
            row=value['release']
            if not row.get('download_url') and row.get('detail_page'):row=resolve_atlas(row,http)
            value['release']=row
            if row.get('download_url'):
                summary['resolved_downloads']+=1
                if control['mode']=='publish':
                    result=publish_release(db,device,row);value['publication']=result
                    if result['status'] in ('published_new','preserved_existing'):summary['published']+=1
                    else:bump(summary,result['status'])
            else:bump(summary,row.get('resolution','UnresolvedDownload'))
            value['done']=True
        except Exception as exc:
            value['attempts']=value.get('attempts',0)+1
            value.update(done=value['attempts']>=3,error=type(exc).__name__)
            if value['done']:bump(summary,type(exc).__name__)
        save(db,key,value);processed+=1
        progress('task_checkpoint_saved',vendor=vendor,processed=processed)
    control['generation']+=1
    control['metadata_requests_this_batch']=len(http.snapshots)
    control['source_snapshots_this_batch']=http.snapshots
    control['state']='working' if tasks(db,run,'model',1) or tasks(db,run,'release',1) else 'finished'
    save(db,run,control)
    return control['state']=='working'


def report(db,control):
    # No links, firmware identifiers, signatures or device lists in public logs.
    report={k:v for k,v in control.items() if k in ('run','generation','state','mode','sha')}
    report['manufacturers']=control['summary']
    report['firmware_extraction_verified']=False
    report['complete']=control['state']=='finished' and all(s.get('models',0)>0 and s.get('release_records',0)>0 and not s['errors'] and not s.get('discovery_errors') and not s.get('firmware_blocker') and s.get('status')!='deferred_by_user' for s in control['summary'].values())
    print(json.dumps(report,sort_keys=True))
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'],'a') as f:f.write('## OEM catalogue — metadata only, NOT image acceptance\n\n```json\n'+json.dumps(report,indent=2)+'\n```\n')
    save(db,control['run']+':report',report)
    return report


def continuation(control):
    with httpx.Client(timeout=30,follow_redirects=False) as client:
        response=client.post('https://api.github.com/repos/'+os.environ['GITHUB_REPOSITORY']+'/actions/workflows/oem_catalogue.yml/dispatches',
            headers={'Authorization':'Bearer '+os.environ['GH_WORKFLOW_TOKEN'],'Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28'},
            json={'ref':control['ref'],'inputs':{'run_id':control['run'],'generation':str(control['generation']),'brands':','.join(control['brands']),'mode':control['mode']}})
        if response.status_code not in (200,204):raise RuntimeError('Catalogue continuation rejected')


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--brands',default='web');parser.add_argument('--mode',choices=['audit','publish'],default='audit')
    parser.add_argument('--run-id',default='');parser.add_argument('--generation',type=int,default=0)
    args=parser.parse_args(argv);db=None;control=None
    stopped=threading.Event()
    worker=threading.Thread(target=heartbeat,args=(stopped,),daemon=True)
    worker.start()
    progress('process_started')
    try:
        if os.environ.get('GITHUB_ACTIONS')!='true' or os.environ.get('GITHUB_REF_TYPE')!='tag':raise ValueError('Requires cloud execution on a frozen tag')
        run=args.run_id or 'oem_'+os.environ['GITHUB_RUN_ID']
        if not re.fullmatch('[a-zA-Z0-9_-]{3,60}',run):raise ValueError('Invalid run id')
        db=D1Client(cf_account_id=os.environ['CF_ACCOUNT_ID'],cf_api_token=os.environ['CF_API_TOKEN'],cf_database_id=os.environ['CF_D1_DATABASE_ID'])
        ensure_base_brands_and_anchor(db)
        http=MetadataHTTP()
        try:
            if not args.run_id:
                brands=list(WEB_BRANDS if args.brands=='web' else ALL if args.brands=='all' else dict.fromkeys(args.brands.split(',')))
                control=start(db,run,brands,args.mode,http)
            else:
                control=get(db,run)
                if not control or control['sha']!=os.environ['GITHUB_SHA']:raise ValueError('Snapshot mismatch')
                if args.generation!=control['generation']:print('{"stale_dispatch":true}');return 0
            if control['generation']>50000:raise ValueError('Finite experiment budget exhausted')
            again=step(db,control,http)
            result=report(db,control)
            if again:continuation(control)
            return 0 if again or result['complete'] else 1
        finally:http.close()
    except Exception as exc:
        print(json.dumps({'error_type':type(exc).__name__}))
        if db and control:
            control['state']='blocked_error';save(db,control['run'],control);report(db,control)
        return 1
    finally:
        stopped.set()
        worker.join(timeout=1)
        if db:db.close()


if __name__=='__main__':raise SystemExit(main())
