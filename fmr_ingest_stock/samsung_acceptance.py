"""Bounded live FOTA/FUS proof, staging one exact current build. No firmware bytes or D1."""
import json
import xml.etree.ElementTree as ET
import httpx
from .staged_catalogue import ROOT,open_local,state_lock,checkpoint_stamp
from .catalogue_store import Store
from .vendor_catalogue import seed_releases
from .samsung_fus import resolve_release


def main():
    model,region='SM-S928B','EUX'
    with httpx.Client(timeout=25,follow_redirects=False,headers={'User-Agent':'Kies2.0_FUS'}) as client:
        response=client.get(f'https://fota-cloud-dn.ospserver.net/firmware/{region}/{model}/version.xml')
        response.raise_for_status()
        if len(response.content)>1024*1024 or b'<!DOCTYPE' in response.content.upper():raise ValueError('Invalid FOTA XML')
        version=ET.fromstring(response.content).findtext('.//latest')
        if not version:raise ValueError('Latest build missing')
        device={'vendor':'samsung','upstream_id':'galaxy-s24-ultra','codename':model,'name':'Galaxy S24 Ultra'}
        release=resolve_release(device,{'vendor':'samsung','device_id':device['upstream_id'],'version':version.strip(),
            'region':region,'kind':'samsung_firmware','android_version':'unknown','metadata_source':'samsung_fota_version_xml'},client)
    path=ROOT/'samsung.sqlite'
    with state_lock(path,'.collect.lock'):
        db=open_local(path)
        try:
            store=Store(db,'samsung')
            # Reuse catalogue identity when present.
            for _,value in store.rows('model'):
                if value['device'].get('codename')==model:device=value['device'];break
            checkpoint_stamp(path,dirty=True)
            seed_releases(store,device,[release])
            # seed_releases defers resolution; commit the proven metadata explicitly.
            for key,value in store.rows('release'):
                if value['release'].get('download_url')==release['download_url']:
                    value.update(status='resolved',error=None,next_check=0)
                    store.put(key,value);break
            print(json.dumps({'event':'samsung_live_fus','model':model,'region':region,'version':release['version'],
                'size':release['refresh_source']['size'],'staged':True,'firmware_bytes':0,'d1_requests':0}),flush=True)
        finally:db.close()
    return 0


if __name__=='__main__':raise SystemExit(main())
