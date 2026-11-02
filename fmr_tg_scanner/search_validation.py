"""Fresh-floor evidence rechecks, explicitly distinct from independent recall."""
import json
import sqlite3
from pathlib import Path
from .message_reader import read_message
from .search_store import SearchStore
from .search_http import SearchHTTP,WorkBudgetReached
from .trial import atomic_json
from .search_policy import analyse
from .search_discovery import aliases


def validate(root,limit=24):
    root=Path(root);products=json.loads((root/'products.json').read_text())['products']
    # A separate transport database makes these actual re-reads, not the
    # collector's own cached HTML. Round-robin models in the validation sample.
    groups={}
    for product in products:groups.setdefault(product['package']['device_codename'],[]).append(product)
    selected=[]
    while groups and len(selected)<limit:
        for code in list(groups):
            if len(selected)>=limit:break
            selected.append(groups[code].pop(0))
            if not groups[code]:del groups[code]
    db=SearchStore(root/'validation.sqlite');checks=[]
    source_meta={}
    if (root/'search.sqlite').exists():
        with sqlite3.connect((root/'search.sqlite').resolve().as_uri()+'?mode=ro',uri=True) as source:
            source_meta={key:json.loads(value) for key,value in source.execute('SELECT key,value FROM tg_search_meta') if key=='device_aliases_v3' or key.startswith('profile:')}
    targets=list(dict.fromkeys(item['package']['device_codename'] for item in selected))
    saved=source_meta.get('device_aliases_v3',{})
    for code in targets:saved.setdefault(code,[])
    db.set_meta('device_aliases_v3',saved)
    names=aliases(None,db,targets)
    http=SearchHTTP(db,lambda _:None,max_requests=limit*3,max_seconds=180)
    try:
        for item in selected:
            p=item['package']
            try:
                msg,url=read_message(lambda u:http.get(u,ttl=0),p['channel_username'],p['msg_id'])
                result,reason=analyse(msg,p['device_codename'],names.get(p['device_codename'],[]),names,source_meta.get('profile:'+p['channel_username'],''))
                checks.append({'post_url':p['tg_post_url'],'device':p['device_codename'],
                    'observed_text':msg['text'][:12000],
                    'exact_floor_present':msg['tg_post_url']==p['tg_post_url'],
                    'resource_post_confirmed':bool(result and result['accepted']),'decision':reason,
                    'text_unchanged':__import__('hashlib').sha256(msg['text'].encode()).hexdigest()==p['raw_text_hash'],
                    'author_verified':False})
            except WorkBudgetReached:break
            except Exception as exc:checks.append({'post_url':p['tg_post_url'],'device':p['device_codename'],'error_type':type(exc).__name__})
        report={'scope':'Fresh exact Telegram floor, device evidence and resource-announcement checks; no file-link requirement or file requests.',
                'available_products':len(products),'sample_size':len(checks),'http_requests':http.requests,
                'checks':checks,'passed':sum(c.get('exact_floor_present',False) and c.get('resource_post_confirmed',False) for c in checks),
                'independent_recall_evaluated':False,'binary_downloads':0,'D1_requests':0}
        atomic_json(root/'validation.json',report)
        print(json.dumps({k:v for k,v in report.items() if k!='checks'}),flush=True)
        return report
    finally:http.close();db.close()
