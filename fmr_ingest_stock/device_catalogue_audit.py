"""Read local metadata and compare Google coverage with both official sources.

No D1 clients, database writes, firmware downloads or encrypted artifact reads.
"""
import json
import os
from pathlib import Path
import sqlite3
import re
from bs4 import BeautifulSoup
from .catalogue_store import KIND
from .device_identity import marketing_name
from .oem_sources import google_catalogue,MetadataHTTP


def models(vendor):
    path=Path(os.environ['FMR_CATALOGUE_STATE_DIR'])/(vendor+'.sqlite')
    if not path.exists():return []
    with sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True) as db:
        values=db.execute("SELECT last_cursor_val FROM crawler_sources WHERE source_kind=? AND source_target GLOB ?",
                          (KIND,'v2:'+vendor+':model:*'))
        return [json.loads(value) for (value,) in values]


def main():
    for vendor in ('xiaomi','google','samsung','oneplus','oppo','vivo','realme','motorola','sony','asus','nothing'):
        items=models(vendor)
        active=[v for v in items if v.get('status')!='excluded']
        print(json.dumps({'vendor':vendor,'local_models':len(active),
            'states':{state:sum(v.get('status')==state for v in active) for state in sorted({v.get('status','unknown') for v in active})},
            'name_examples':[{'before':v['device'].get('name'),'after':marketing_name(v['device'])} for v in active[:3]]},ensure_ascii=False),flush=True)
    class AuditHTTP(MetadataHTTP):
        def __init__(self):super().__init__();self.headers=set()
        def get(self,url,**kwargs):
            body=super().get(url,**kwargs)
            for heading in BeautifulSoup(body,'html.parser').find_all(['h2','h3','h4']):
                code=heading.get('id','')
                if re.fullmatch('[a-z0-9-]{2,32}',code) and re.search(r'\b(?:Pixel|Nexus)\b',heading.get_text(' ',strip=True)):
                    self.headers.add(code)
            return body
    http=AuditHTTP()
    try:devices,releases,errors=google_catalogue(http)
    finally:http.close()
    known={v['device']['upstream_id'] for v in models('google') if v.get('status')!='excluded'}
    expected={d['upstream_id'] for d in devices}
    report={'google_official':{'firmware_targets':len(devices),'release_records':sum(map(len,releases.values())),
        'device_headings':len(http.headers),'headings_without_parsed_firmware':sorted(http.headers-expected),
        'source_errors':errors,'missing_from_collector':[{'target':d['upstream_id'],'name':marketing_name(d)} for d in devices if d['upstream_id'] not in known],
        'collector_only':sorted(known-expected),'names':[{'target':d['upstream_id'],'name':marketing_name(d)} for d in devices]}}
    print(json.dumps(report,ensure_ascii=False),flush=True)
    summary=os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:Path(summary).write_text('# Device catalogue coverage\n\n```json\n'+json.dumps(report,ensure_ascii=False,indent=2)+'\n```\n\nRead-only local state and official metadata. No D1 or firmware requests.\n')
    return 0


if __name__=='__main__':raise SystemExit(main())
