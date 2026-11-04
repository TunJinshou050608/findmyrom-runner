"""Resident query-first trial using the existing private checkpoint/handoff loop."""
import argparse
import json
import os
from pathlib import Path
import signal
import re
import time
from .search_engine import run
from .search_store import SearchStore
from .trial import atomic_json
from .watch import watch
from .targets import read_catalogue
from .website_order import read_order
from .device_schedule import KEY as SCHEDULE_KEY, select, complete


def produce(root, *, shard=None, shards=4):
    root=Path(root)
    config_path=root/'tg_search_config.json'
    config=json.loads(config_path.read_text()) if config_path.exists() else {'max_requests':100,'max_seconds':360}
    # Old four-device configs must not silently restrict automatic discovery.
    config.pop('targets',None)
    config['target_source']='local_catalogue'
    names,sources=read_catalogue()
    atomic_json(config_path,config)
    state_root=root/'search-v3';state_root.mkdir(exist_ok=True)
    store=SearchStore(state_root/'search.sqlite')
    try:
        ordered,order_info=read_order(store,names)
        if shard is not None:
            from .local_runtime import partition
            ordered=[code for code in ordered if partition(code,shards)==shard]
        schedule=store.meta(SCHEDULE_KEY,{})
        targets,reset=select(ordered,schedule,time.time(),config.get('target_batch_size',8))
        if not targets:
            report={'ok':True,'activity':'all_devices_cooling','targets':[],
                    'target_selection':{**order_info,'total_devices':len(ordered),'batch_devices':0},
                    'next_device_at':min(v['next_at'] for k,v in schedule['devices'].items() if k in ordered)}
            atomic_json(root/'reports/latest.json',report)
            print(json.dumps(report),flush=True)
            return report
        print(json.dumps({'event':'device_batch_selected','source':'website_brand_pages','targets':targets,'order_refreshed_at':order_info.get('fetched_at')}),flush=True)
        if not store.meta('prior_lab_import_complete',False):
            imported=[]
            # Warm operation may reuse sources independently discovered by the
            # lab. Never import its accepted products or pagination watermarks:
            # every source and resource faces the current policy again.
            paths=sorted((root/'experiments'/'search-v3').glob('*/query_first/products.json'))[-10:]
            for path in paths:
                for item in json.loads(path.read_text()).get('products',[]):
                    p=item['package'];code=p['device_codename'];channel=p['channel_username']
                    if code not in targets or not re.fullmatch('[a-z][a-z0-9_]{3,31}',channel):continue
                    store.enqueue('admit',code,{'channel':channel,'depth':0},20)
                    imported.append({'device':code,'channel':channel,'evidence_post':p['tg_post_url'],'lab_path':str(path.relative_to(root))})
            store.set_meta('prior_lab_source_evidence',imported)
            store.set_meta('prior_lab_import_complete',True)
    finally:store.close()
    fetch_llm=os.environ.get('FMR_TG_FETCH_LLM','false').lower()=='true'
    report=run(root/'search-v3',targets,max_requests=config.get('max_requests',100),max_seconds=config.get('max_seconds',360),
               max_tasks_per_device=config.get('max_tasks_per_device',24),catalogue_names=names,
               enable_llm=fetch_llm,max_llm_calls=50 if fetch_llm else 0)
    # Only advance after a completed batch has committed its frontier/report.
    store=SearchStore(state_root/'search.sqlite')
    try:
        store.set_meta(SCHEDULE_KEY,complete(schedule,targets,report,time.time(),reset))
        store.set_meta('catalogue_last_device',targets[-1])
    finally:store.close()
    report['target_selection']={**order_info,'total_devices':len(ordered),'batch_devices':len(targets),'sources':sources,
                                'round':schedule['round'],'devices':targets}
    report['device_schedule']={code:schedule['devices'].get(code,{}) for code in targets}
    print(json.dumps({'event':'device_batch_yielded','devices':report['device_schedule']}),flush=True)
    report['products_path']='search-v3/products.json'
    report['candidates_path']='search-v3/candidates.json'
    atomic_json(root/'reports/latest.json',report)
    return report


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--state-dir',required=True);args=parser.parse_args()
    if os.environ.get('GITHUB_ACTIONS')!='true':parser.error('Live network work runs on GitHub Actions')
    def stop(*_):raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM,stop)
    result=watch(args.state_dir,producer=produce)
    print(json.dumps({'event':'search_resident_finished',**result}),flush=True)
    return 0 if result['last_data_ok'] else 1


if __name__=='__main__':
    try:raise SystemExit(main())
    except KeyboardInterrupt:raise SystemExit(130)
