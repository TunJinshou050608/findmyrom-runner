"""Automatic local catalogue enrollment, rotation and restart semantics."""
import json
import sqlite3
import pytest
from fmr_tg_scanner.targets import read_catalogue, select_targets
from fmr_ingest_stock.catalogue_store import KIND
from fmr_tg_scanner.search_store import SearchStore


def catalogue(path, entries):
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE crawler_sources(device_codename TEXT,source_kind TEXT,source_target TEXT,last_cursor_val TEXT)')
        for i, entry in enumerate(entries):
            db.execute('INSERT INTO crawler_sources VALUES (?,?,?,?)',('__global__',KIND,f'v2:test:model:{i}',json.dumps(entry)))


def test_reads_models_and_aliases_without_release_scan(tmp_path):
    path=tmp_path/'xiaomi.sqlite'
    catalogue(path,[
        {'device':{'vendor':'xiaomi','upstream_id':'alioth','name':'POCO F3 / Redmi K40','codename':'alioth','models':'M2012K11AC'}},
        {'device':{'vendor':'google','upstream_id':'panther','name':'Pixel 7','codename':'panther'}},
        {'status':'excluded','device':{'vendor':'asus','upstream_id':'chrome','name':'Chromebook'}},
        {'device':{'vendor':'asus','upstream_id':'chrome2','catalogue_scope':'non_android','name':'Chromebook'}},
    ])
    with sqlite3.connect(path) as db:
        db.execute('INSERT INTO crawler_sources VALUES (?,?,?,?)',('__global__',KIND,'v2:test:release:1','not model JSON'))
    names,sources=read_catalogue(tmp_path)
    assert set(names)=={'alioth','google-panther'}
    assert 'POCO F3 / Redmi K40' in names['alioth'] and 'M2012K11AC' in names['alioth']
    assert 'Redmi K40' in names['alioth'] and 'POCO F3' in names['alioth']
    assert 'panther' in names['google-panther'] and 'Google Pixel 7' in names['google-panther']
    assert sources[0]['devices_read']==2


def test_empty_catalogue_has_no_manual_four_device_fallback(tmp_path):
    with pytest.raises(RuntimeError,match='missing or empty'):read_catalogue(tmp_path)


def test_published_device_fallback_ignores_system_anchor(tmp_path):
    with sqlite3.connect(tmp_path/'published.sqlite') as db:
        db.execute('CREATE TABLE devices(codename TEXT,marketing_name TEXT,model_numbers_json TEXT)')
        db.executemany('INSERT INTO devices VALUES (?,?,?)',[
            ('__global__','Global System Anchor','[]'),('alioth','POCO F3','["M2012K11AC"]')])
    names,_=read_catalogue(tmp_path)
    assert set(names)=={'alioth'}


def test_rotation_handles_new_and_removed_devices():
    names={f'device{i:04}':[] for i in range(1200)}
    selected=[];last=None
    for _ in range(150):
        batch=select_targets(names,last,8);selected.extend(batch);last=batch[-1]
    assert len(set(selected))==1200
    assert select_targets(names,last,8)==selected[:8]
    names['device1200']=[]
    assert select_targets(names,last,8)[0]=='device1200'
    assert select_targets({'aaa':[],'ccc':[]},'bbb',1)==['ccc']


def test_resident_ignores_old_manual_targets_and_enrolls_new_devices(tmp_path,monkeypatch):
    from fmr_tg_scanner import search_watch
    root=tmp_path/'state';root.mkdir()
    (root/'tg_search_config.json').write_text(json.dumps({'targets':['obsolete'],'target_batch_size':1}))
    names={'alioth':['POCO F3'],'liuqin':['Xiaomi Pad 6 Pro']}
    monkeypatch.setattr(search_watch,'read_catalogue',lambda:(dict(names),[]))
    seen=[]
    def run(path,targets,**kwargs):
        seen.append(targets)
        assert kwargs['catalogue_names']==names
        return {'ok':True,'packages':0}
    monkeypatch.setattr(search_watch,'run',run)
    search_watch.produce(root)
    names['annibale']=['Redmi K90']
    report=search_watch.produce(root)
    search_watch.produce(root)
    assert seen==[['alioth'],['annibale'],['liuqin']]
    assert report['target_selection']['total_devices']==3
    assert 'targets' not in json.loads((root/'tg_search_config.json').read_text())


def test_failed_batch_does_not_advance_catalogue_cursor(tmp_path,monkeypatch):
    from fmr_tg_scanner import search_watch
    monkeypatch.setattr(search_watch,'read_catalogue',lambda:({'alioth':['POCO F3']},[]))
    def fail(*a,**k):raise RuntimeError('interrupted')
    monkeypatch.setattr(search_watch,'run',fail)
    with pytest.raises(RuntimeError):search_watch.produce(tmp_path)
    store=SearchStore(tmp_path/'search-v3/search.sqlite')
    assert store.meta('catalogue_last_device') is None
    store.close()

