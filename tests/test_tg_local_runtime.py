import hashlib
import json
import os
from pathlib import Path
import sqlite3

import pytest

from fmr_tg_scanner.local_runtime import initialize, partition, clean_once, lock
from fmr_tg_scanner.search_store import SearchStore
from fmr_tg_scanner.trial import atomic_json


def old_state(root):
    path=root/'search-v3';path.mkdir(parents=True)
    store=SearchStore(path/'search.sqlite')
    for i,code in enumerate(['alioth','annibale','ishtar','liuqin']):
        text='source '+code
        package={'device_codename':code,'delivery_type':'telegram_message','channel_username':'testupdates',
                 'msg_id':i+1,'raw_text_hash':hashlib.sha256(text.encode()).hexdigest()}
        store.save(package,{'observed_text':text},'unknown','accepted',True)
        store.enqueue('admit',code,{'channel':'testupdates'},3)
    store.close()
    (path/'logs').mkdir()
    (path/'logs/legacy.jsonl').write_text('')
    return root


def test_migration_preserves_all_records_and_exclusively_partitions_frontiers(tmp_path):
    source=old_state(tmp_path/'old');root=tmp_path/'local'
    initialize(root,source)
    assert json.loads((root/'initialized.json').read_text())['github_state_writes'] is False
    seen=[]
    for worker in range(4):
        store=SearchStore(root/'workers'/str(worker)/'search-v3/search.sqlite')
        rows=store.query('SELECT device FROM tg_search_records')
        seen.extend(r['device'] for r in rows)
        assert all(partition(r['device'])==worker for r in rows)
        assert all(partition(r['device'])==worker for r in store.query('SELECT device FROM tg_search_tasks'))
        store.close()
    assert sorted(seen)==['alioth','annibale','ishtar','liuqin']
    # Repeated sessions must not restore the old checkpoint over new work.
    marker=root/'workers/0/local-proof';marker.write_text('new work')
    initialize(root,source)
    assert marker.read_text()=='new work'


def test_snapshot_merge_is_complete_and_unchanged_sources_skip_reaudit(tmp_path,monkeypatch):
    root=tmp_path/'local';initialize(root,old_state(tmp_path/'old'))
    calls=[]
    def audit(clean):
        store=SearchStore(clean/'search-v3/search.sqlite')
        rows=store.query('SELECT * FROM tg_search_records')
        assert len(rows)==4
        store.close();calls.append(rows)
        out=clean/'audits'/('full-quality-'+os.environ['GITHUB_RUN_ID'])
        atomic_json(out/'report.json',{'checked':len(rows)})
        return out
    monkeypatch.setattr('fmr_tg_scanner.quality_audit.audit',audit)
    assert clean_once(root)['status']=='audit_ready'
    assert clean_once(root)['status']=='unchanged_sources'
    assert len(calls)==1
    assert (root/'clean/search-v3/logs/legacy.jsonl').is_file()


def test_new_source_revision_is_not_hidden_by_a_later_audit_timestamp(tmp_path,monkeypatch):
    root=tmp_path/'local';initialize(root,old_state(tmp_path/'old'))
    worker_db=root/'workers'/str(partition('alioth'))/'search-v3/search.sqlite'
    with sqlite3.connect(worker_db) as db:
        original=db.execute("SELECT updated FROM tg_search_records WHERE device='alioth'").fetchone()[0]
    versions=[]
    def audit(clean):
        store=SearchStore(clean/'search-v3/search.sqlite')
        row=store.query("SELECT payload FROM tg_search_records WHERE device='alioth'")[0]
        versions.append(json.loads(row['payload']).get('revision',0))
        store.execute('UPDATE tg_search_records SET updated=?',(original+1000,));store.close()
        out=clean/'audits'/('full-quality-'+os.environ['GITHUB_RUN_ID'])
        atomic_json(out/'report.json',{'checked':4})
        return out
    monkeypatch.setattr('fmr_tg_scanner.quality_audit.audit',audit)
    clean_once(root)
    with sqlite3.connect(worker_db) as db:
        value=json.loads(db.execute("SELECT payload FROM tg_search_records WHERE device='alioth'").fetchone()[0])
        value['revision']=1
        db.execute("UPDATE tg_search_records SET payload=?,updated=? WHERE device='alioth'",(json.dumps(value),original+1))
    clean_once(root)
    assert versions==[0,1]


def test_role_locks_allow_parallel_workers_but_not_two_writers_to_one_shard(tmp_path):
    with lock(tmp_path,'fetch-0'):
        with pytest.raises(BlockingIOError):
            with lock(tmp_path,'fetch-0'):pass
        with lock(tmp_path,'fetch-1'):pass
        with lock(tmp_path,'clean'):pass


def test_production_workers_never_select_the_same_device(tmp_path,monkeypatch):
    from fmr_tg_scanner import search_watch
    names={f'device{i:04}':[] for i in range(100)}
    monkeypatch.setattr(search_watch,'read_catalogue',lambda:(names,[]))
    monkeypatch.setattr(search_watch,'read_order',lambda store,names:(list(names),{'source':'website_brand_pages'}))
    selected=[]
    def run(root,targets,**kwargs):
        selected.extend(targets)
        return {'ok':True,'packages':0}
    monkeypatch.setattr(search_watch,'run',run)
    for worker in range(4):
        root=tmp_path/str(worker);root.mkdir()
        report=search_watch.produce(root,shard=worker,shards=4)
        assert all(partition(code)==worker for code in report['target_selection']['devices'])
    assert len(selected)==32 and len(set(selected))==32
