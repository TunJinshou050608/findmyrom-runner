"""Fresh-floor evidence rechecks, explicitly distinct from independent recall."""
import json
from pathlib import Path
from .message_reader import read_message
from .search_store import SearchStore
from .search_http import SearchHTTP,WorkBudgetReached
from .trial import atomic_json


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
    http=SearchHTTP(db,lambda _:None,max_requests=limit*3,max_seconds=180)
    try:
        for item in selected:
            p=item['package']
            try:
                msg,url=read_message(lambda u:http.get(u,ttl=0),p['channel_username'],p['msg_id'])
                wanted=set(json.loads(p['download_links_json']))
                actual=set(msg['links'])
                if msg.get('has_tg_attachment') and msg.get('file_attachment_name')==p.get('file_attachment_name'):actual.add(msg['tg_post_url'])
                checks.append({'post_url':p['tg_post_url'],'device':p['device_codename'],
                    'exact_floor_present':True,'download_links_still_in_post':wanted.issubset(actual),
                    'text_unchanged':__import__('hashlib').sha256(msg['text'].encode()).hexdigest()==p['raw_text_hash'],
                    'author_verified':False,'binary_verified':False})
            except WorkBudgetReached:break
            except Exception as exc:checks.append({'post_url':p['tg_post_url'],'device':p['device_codename'],'error_type':type(exc).__name__})
        report={'scope':'Fresh public-post identity, text and download-link evidence; not independent device precision or recall.',
                'available_products':len(products),'sample_size':len(checks),'http_requests':http.requests,
                'checks':checks,'passed':sum(c.get('exact_floor_present',False) and c.get('download_links_still_in_post',False) for c in checks),
                'independent_recall_evaluated':False,'binary_downloads':0,'D1_requests':0}
        atomic_json(root/'validation.json',report)
        print(json.dumps({k:v for k,v in report.items() if k!='checks'}),flush=True)
        return report
    finally:http.close();db.close()
