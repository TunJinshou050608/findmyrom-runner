"""One startup handoff; never polls running jobs or resumes old D1-heavy jobs."""
import os,time
import httpx
from .d1_budget import NOT_BEFORE

VENDORS=('xiaomi','google','motorola','oneplus','nothing','oppo','realme','asus','samsung','vivo','sony')


def main():
    if time.time()<NOT_BEFORE:
        print('D1 quota has not reset; no workflows or database are touched.',flush=True);return 0
    base='https://api.github.com/repos/'+os.environ['GITHUB_REPOSITORY']+'/actions/workflows/'
    with httpx.Client(timeout=30,headers={'Authorization':'Bearer '+os.environ['GH_WORKFLOW_TOKEN'],'Accept':'application/vnd.github+json'}) as client:
        for name in [*("catalogue_"+v+".yml" for v in (*VENDORS,'huawei','honor')), 'catalogue_publish.yml']:
            response=client.get(base+name);response.raise_for_status()
            if response.json().get('state')!='active':raise RuntimeError('OperatorMustEnableWorkflow:'+name)
        # Publish migrated records first, before collectors occupy a local runner.
        response=client.post(base+'catalogue_publish.yml/dispatches',json={'ref':'main'})
        response.raise_for_status();print('Ten-minute publisher handed off; no status polling.',flush=True)
        for vendor in VENDORS:
            response=client.post(base+'catalogue_'+vendor+'.yml/dispatches',json={'ref':'main','inputs':{'mode':'audit','operation':'incremental','cycle':'','batch':'1'}})
            response.raise_for_status();print(vendor+': staged incremental collector handed off (zero D1 requests)',flush=True)
    return 0


if __name__=='__main__':raise SystemExit(main())
