"""Isolated warm-pool regression for unresolved devices, with no channel seeds."""
import json
import os
from pathlib import Path
import sqlite3
from .search_store import SearchStore
from .search_engine import run
from .search_validation import validate
from .trial import atomic_json
from .watch import publish_private


def focus(state_root,targets):
    state_root=Path(state_root).resolve()
    root=state_root/'experiments/search-focus'/os.environ.get('GITHUB_RUN_ID','local')
    root.mkdir(parents=True,exist_ok=True)
    original=state_root/'search-v3/search.sqlite'
    diagnosis={'targets':targets,'warm_source_pool':True,'manual_channel_seeds':[],
               'source_of_pool':'Resident crawler metadata; no product records or cursors imported.'}
    dest=SearchStore(root/'search.sqlite')
    try:
        if original.exists():
            with sqlite3.connect(original.as_uri()+'?mode=ro',uri=True) as src:
                for key,value in src.execute('SELECT key,value FROM tg_search_meta'):
                    if key in ('sources','device_aliases_v3') or key.startswith('profile:'):dest.set_meta(key,json.loads(value))
                diagnosis['resident_frontier']=[dict(zip(('device','kind','count','errors'),row)) for row in src.execute('SELECT device,kind,COUNT(*),SUM(last_error IS NOT NULL) FROM tg_search_tasks GROUP BY device,kind') if row[0] in targets]
                diagnosis['resident_resource_decisions']=[dict(zip(('device','accepted','reason','count'),row)) for row in src.execute('SELECT device,accepted,reason,COUNT(*) FROM tg_search_records GROUP BY device,accepted,reason') if row[0] in targets]
        diagnosis['imported_sources']=len(dest.meta('sources',{}))
        diagnosis['assigned_source_profiles']={code:[{'channel':channel,'strategy':info.get('strategy_by_device',{}).get(code),'profile':dest.meta('profile:'+channel,'')[:1600]} for channel,info in dest.meta('sources',{}).items() if code in info.get('targets',[])] for code in targets}
    finally:dest.close()
    atomic_json(root/'diagnosis.json',diagnosis);publish_private(state_root)
    first=run(root,targets,max_requests=180,max_seconds=480)
    atomic_json(root/'first_batch.json',first);publish_private(state_root)
    second=run(root,targets,max_requests=90,max_seconds=240)
    atomic_json(root/'continuation.json',second)
    validate(root)
    publish_private(state_root)
    return second
