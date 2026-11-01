"""Resident query-first trial using the existing private checkpoint/handoff loop."""
import argparse
import json
import os
from pathlib import Path
import signal
from .search_engine import run
from .trial import atomic_json
from .watch import watch


def produce(root):
    root=Path(root)
    config_path=root/'tg_search_config.json'
    config=json.loads(config_path.read_text()) if config_path.exists() else {
        'targets':['annibale','ishtar','liuqin','alioth'],'max_requests':100,'max_seconds':360}
    targets=config['targets']
    if not isinstance(targets,list) or len(set(targets))!=len(targets):raise ValueError('Invalid search targets')
    atomic_json(config_path,config)
    report=run(root/'search-v3',targets,max_requests=config.get('max_requests',100),max_seconds=config.get('max_seconds',360))
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
