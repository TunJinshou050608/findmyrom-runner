"""Finite, private metadata experiments. No D1 or firmware credentials."""
import argparse
import os
from pathlib import Path
from .search_engine import run
from .trial import atomic_json
from .search_validation import validate
from .watch import publish_private


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--state-dir',required=True)
    parser.add_argument('--targets',default='annibale,ishtar,liuqin,alioth')
    parser.add_argument('--mode',choices=('batch','compare'),default='batch')
    args=parser.parse_args()
    targets=list(dict.fromkeys(v.strip().lower() for v in args.targets.split(',') if v.strip()))
    root=Path(args.state_dir)/'experiments'/'search-v3'/os.environ.get('GITHUB_RUN_ID','local')
    modes=['query_first'] if args.mode=='batch' else ['query_first','latest_first']
    reports={}
    for mode in modes:
        reports[mode]=run(root/mode,targets,max_requests=180,max_seconds=480,mode=mode)
        atomic_json(root/mode/'cold_report.json',reports[mode])
        publish_private(Path(args.state_dir).resolve())
    resume=run(root/'query_first',targets,max_requests=90,max_seconds=240,mode='query_first')
    atomic_json(root/'resume.json',{'report':resume,'restored_existing_frontier':True,'manual_channel_seeds':[],
        'same_state_as':'query_first','excluded_from_ordering_comparison':True})
    for mode in modes:validate(root/mode)
    atomic_json(root/'comparison.json',{'reports':reports,'same_request_budget':180,
        'same_time_budget_seconds':480,'channel_seeds':[],
        'comparison_scope':'Search ordering ablation using the same new discovery and policy; not a frozen legacy baseline.',
        'independent_recall_evaluated':False})


if __name__=='__main__':main()
