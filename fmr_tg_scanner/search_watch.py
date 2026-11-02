"""Resident query-first trial using the existing private checkpoint/handoff loop."""
import argparse
import json
import os
from pathlib import Path
import signal
import re
from .search_engine import run
from .search_store import SearchStore
from .trial import atomic_json
from .watch import watch
from .targets import read_catalogue, select_targets


def produce(root):
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
        targets=select_targets(names,store.meta('catalogue_last_device'),config.get('target_batch_size',8))
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
    report=run(root/'search-v3',targets,max_requests=config.get('max_requests',100),max_seconds=config.get('max_seconds',360),catalogue_names=names)
    # Only advance after a completed batch has committed its frontier/report.
    store=SearchStore(state_root/'search.sqlite')
    try:store.set_meta('catalogue_last_device',targets[-1])
    finally:store.close()
    report['target_selection']={'source':'local_catalogue','total_devices':len(names),'batch_devices':len(targets),'sources':sources}
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
