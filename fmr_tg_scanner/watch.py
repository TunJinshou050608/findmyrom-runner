"""Bounded resident trial checks with durable cadence and private per-cycle commits."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import time

from .trial import run,atomic_json

PERIOD=600
SESSION_SECONDS=3300


def publish_private(root):
    def git(*args,check=True):
        result=subprocess.run(['git','-C',str(root),*args],capture_output=True,text=True,timeout=60)
        if check and result.returncode:raise RuntimeError('Private checkpoint Git '+args[0]+' failed (exit '+str(result.returncode)+')')
        return result
    paths=[str(p.relative_to(root)) for p in root.rglob('*') if p.is_file() and '.git' not in p.relative_to(root).parts and p.suffix in ('.sqlite','.json','.jsonl')]
    if paths:git('add','--',*paths)
    if git('diff','--cached','--quiet',check=False).returncode==0:return False
    # Rebase creates commits too. Command-scoped identity on the original
    # commit alone fails as soon as another checkpoint advances origin/main.
    git('config','--local','user.name','FindMyROM')
    git('config','--local','user.email','automation@findmyrom.com')
    git('-c','user.name=FindMyROM','-c','user.email=automation@findmyrom.com','commit','--quiet','-m','TG01 resident check: private metadata and audit')
    for _attempt in range(3):
        git('pull','--rebase','--quiet','origin','main')
        if git('push','--quiet','origin','HEAD:main',check=False).returncode==0:return True
    raise RuntimeError('Private checkpoint push failed after three attempts')


def watch(root,*,duration=SESSION_SECONDS,period=PERIOD,producer=run,publisher=publish_private,clock=time.time,sleeper=time.sleep):
    root=Path(root).resolve();path=root/'monitor_state.json'
    state=json.loads(path.read_text()) if path.exists() else {}
    started=clock();deadline=started+duration;run_id=os.environ.get('GITHUB_RUN_ID','local-test')
    next_due=state.get('next_check_at',started)
    # A broken clock or stale future state must not postpone checks indefinitely.
    next_due=min(next_due,started+period)
    state.update(session_run_id=run_id,session_started_at=started,handoff_ready=False)
    atomic_json(path,state);publisher(root)
    cycles=0;last_ok=state.get('last_data_ok',False);normal_end=False
    try:
        while clock()<deadline:
            now=clock()
            if now<next_due:
                delay=min(30,next_due-now,deadline-now)
                print(json.dumps({'event':'watch_heartbeat','next_check_in_seconds':round(next_due-now),'session_remaining_seconds':round(deadline-now)}),flush=True)
                sleeper(delay);continue
            sequence=state.get('cycle_sequence',0)
            os.environ['FMR_TG_CYCLE']=str(sequence)
            cycle_started=clock()
            print(json.dumps({'event':'resident_check_started','cycle':sequence}),flush=True)
            try:
                report=producer(root);last_ok=bool(report['ok'])
                print(json.dumps({'event':'resident_check_finished','cycle':sequence,'data_ok':last_ok,'packages':report['packages']}),flush=True)
            except Exception as exc:
                last_ok=False
                atomic_json(root/'reports'/f'watch-error-{run_id}-{sequence}.json',{'ok':False,'error_type':type(exc).__name__,'timestamp':clock(),'cycle':sequence})
                print(json.dumps({'event':'resident_check_failed','cycle':sequence,'error_type':type(exc).__name__}),flush=True)
            next_due=cycle_started+period
            state.update(last_check_at=cycle_started,next_check_at=next_due,cycle_sequence=sequence+1,last_data_ok=last_ok)
            atomic_json(path,state);publisher(root);cycles+=1
        normal_end=True
    finally:
        os.environ.pop('FMR_TG_CYCLE',None)
        state.update(handoff_ready=normal_end,session_ended_at=clock(),last_data_ok=last_ok)
        atomic_json(path,state)
        # Includes the successful page transactions from an interrupted cycle.
        publisher(root)
    return {'cycles':cycles,'last_data_ok':last_ok,'handoff_ready':normal_end}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--state-dir',required=True);args=parser.parse_args()
    if os.environ.get('GITHUB_ACTIONS')!='true':parser.error('Resident live checks run only on GitHub Actions')
    def stop(*_args):raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM,stop)
    result=watch(args.state_dir)
    print(json.dumps({'event':'resident_session_finished',**result}),flush=True)
    return 0 if result['last_data_ok'] else 1


if __name__=='__main__':
    try:raise SystemExit(main())
    except KeyboardInterrupt:
        print(json.dumps({'event':'resident_session_stopped'}),flush=True);raise SystemExit(130)
