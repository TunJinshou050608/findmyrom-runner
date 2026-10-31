"""One handoff after the user-specified UTC reset; never polls running jobs."""
from datetime import datetime,timezone
import json,os,time
import httpx
from .d1_budget import NOT_BEFORE

VENDORS=('xiaomi','google','motorola','oneplus','nothing','oppo','realme','asus','samsung','vivo','sony')


def main():
    if time.time()<NOT_BEFORE:
        print('D1 quota has not reset; no workflows or database are touched.',flush=True);return 0
    repository=os.environ['GITHUB_REPOSITORY']
    with httpx.Client(timeout=30,headers={'Authorization':'Bearer '+os.environ['GH_WORKFLOW_TOKEN'],'Accept':'application/vnd.github+json'}) as client:
        base='https://api.github.com/repos/'+repository+'/actions/workflows/'
        for vendor in VENDORS:
            url=base+'catalogue_'+vendor+'.yml'
            response=client.put(url+'/enable');response.raise_for_status()
            response=client.post(url+'/dispatches',json={'ref':'main','inputs':{'mode':'publish','operation':'incremental','cycle':'','batch':'1'}})
            response.raise_for_status();print(vendor+': incremental publish handoff accepted',flush=True)
        # Manual-only source diagnostics; no automatic repeated blocked requests.
        for vendor in ('huawei','honor'):
            response=client.put(base+'catalogue_'+vendor+'.yml/enable');response.raise_for_status()
        # One indexed control read, only after the UTC reset. Retain the frozen
        # extraction ref/SHA and exact saved generation; no algorithm edits.
        from fmr_core.d1_client import D1Client
        db=D1Client(cf_account_id=os.environ['CF_ACCOUNT_ID'],cf_api_token=os.environ['CF_API_TOKEN'],cf_database_id=os.environ['CF_D1_DATABASE_ID'])
        try:
            rows=db.query("SELECT last_cursor_val FROM crawler_sources WHERE device_codename='__global__' AND source_kind='history_sweep' AND source_target=?",('hs_41189187836',))
        finally:db.close()
        if rows:
            control=json.loads(rows[0]['last_cursor_val'])
            if control.get('state') not in ('finished','completed'):
                response=client.put(base+'history_sweep.yml/enable');response.raise_for_status()
                response=client.post(base+'history_sweep.yml/dispatches',json={'ref':control['ref'],'inputs':{'run_id':control['run_id'],'generation':str(control['generation'])}})
                response.raise_for_status();print('Frozen image test resumed from its saved checkpoint; original tag retained.',flush=True)
    print('No running-job status was polled.',flush=True)
    return 0


if __name__=='__main__':raise SystemExit(main())
