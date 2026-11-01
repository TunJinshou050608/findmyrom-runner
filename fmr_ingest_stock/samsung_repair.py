"""Correct paused Samsung state and test at most two real metadata requests."""
from collections import Counter
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET

from .staged_catalogue import ROOT,open_local,encrypt_file,state_lock
from .catalogue_store import Store
from .vendor_catalogue import upgrade_adapters,Status,problem
from .oem_sources import MetadataHTTP
from .samsung_fus import SmartFUS


def main():
    path=ROOT/'samsung.sqlite'
    if not path.exists():raise RuntimeError('Samsung local checkpoint is missing')
    with state_lock(path,'.source-repair.lock'):
        db=open_local(path,initialize=False)
        try:
            store=Store(db,'samsung')
            with Status('samsung') as status:upgrade_adapters(store,status)
            control=store.get(store.key('control')) or {}
            control.update(state='paused',continuation_pending=False,paused_by_user=True)
            store.put(store.key('control'),control)
            candidates=[];seen=set();errors=Counter()
            for _,value in store.rows('release'):
                row=value['release'];code=value['device'].get('codename','');region=row.get('region','')
                if value.get('error'):errors[value['error']]+=1
                if re.fullmatch('SM-[A-Z0-9]+',code) and re.fullmatch('[A-Z0-9]{3}',region) and code not in seen:
                    candidates.append((code,region));seen.add(code)
            print(json.dumps({'event':'samsung_state_repaired','counts':store.counts(),'errors':dict(errors),
                'paused':True,'d1_requests':0},sort_keys=True),flush=True)
        finally:db.close();encrypt_file('samsung',path)
    # Independent bounded proof only. This does not execute the crawler queue.
    for model,region in candidates[:2]:
        http=MetadataHTTP();fus=SmartFUS()
        try:
            body=http.get(f'https://fota-cloud-dn.ospserver.net/firmware/{region}/{model}/version.xml')
            if b'<!DOCTYPE' in body.upper() or b'<!ENTITY' in body.upper():raise ValueError('Unsafe Samsung XML')
            version=ET.fromstring(body).findtext('.//latest')
            if not version:raise ValueError('No current FOTA version')
            result=fus.binary_info(model,region,version.strip())
            print(json.dumps({'event':'samsung_fus_probe','metadata_verified':True,'size':result['size'],
                'exact_version':True,'binary_downloaded':False,'download_service_required':True}),flush=True)
        except Exception as exc:
            print(json.dumps({'event':'samsung_fus_probe','error':problem(exc),
                'reason':str(exc) if type(exc) is ValueError else None,'binary_downloaded':False}),flush=True)
        finally:http.close();fus.close()
    return 0


if __name__=='__main__':raise SystemExit(main())
