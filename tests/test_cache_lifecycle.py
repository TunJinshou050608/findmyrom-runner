import json
import sqlite3
import time
from pathlib import Path

import httpx
import pytest

from fmr_ingest_stock.cache_lifecycle import ByteLRU,PageCache
from fmr_ingest_stock.catalogue_store import Store,KIND
from fmr_ingest_stock import staged_catalogue as staged
from test_staged_catalogue import env


def test_byte_lru_accounts_for_replacements_and_large_bodies():
    cache=ByteLRU(10)
    cache['a']=b'aaaa';cache['b']=b'bbbb'
    assert cache.get('a')==b'aaaa'
    cache['c']=b'cccc'
    assert 'b' not in cache and cache.used==8
    cache['a']=b'a';assert cache.used==5
    cache['oversize']=bytes(11)
    assert 'oversize' not in cache and cache.used==5
    cache.clear();assert cache.used==0


def test_disk_cache_expiry_and_lru_never_remove_model_release_or_control(env):
    db,_=env;store=Store(db,'xiaomi');now=time.time()
    permanent={store.key(stage,stage):{'status':'pending','proof':stage} for stage in ['model','release','control']}
    store.put_many(list(permanent.items()))
    for n in range(8):
        store.put(store.key('page',n),{'body':'x'*100,'checked_at':now,'retained_until':now+(1000 if n else -1)})
    cache=PageCache(store,budget=600,max_entries=3)
    assert cache.bytes<=600 and len(cache.entries)<=3 and cache.evicted>=5
    for key,value in permanent.items():assert store.get(key)==value
    cache.put(store.key('page','new'),{'body':'y'*100,'checked_at':now,'retained_until':now+1000})
    assert cache.bytes<=600 and len(cache.entries)<=3
    with pytest.raises(ValueError):cache.put(store.key('model','bad'),{'checked_at':now})


def test_checkpoint_excludes_cache_but_live_cache_and_progress_survive(env,tmp_path,monkeypatch):
    db,path=env;root=tmp_path/'exports';root.mkdir();monkeypatch.setattr(staged,'ROOT',root)
    store=Store(db,'xiaomi')
    page=store.key('page','old');release=store.key('release','done')
    store.put(page,{'body':'disposable'});store.put(release,{'status':'resolved','proof':'keep'})
    staged.encrypt_file('xiaomi',path)
    restored=tmp_path/'restored.sqlite'
    restored.write_bytes(staged.unseal((root/'upload/xiaomi.sqlite.enc').read_bytes(),'xiaomi'))
    with sqlite3.connect(restored) as copy:
        assert copy.execute('SELECT COUNT(*) FROM crawler_sources WHERE source_kind=? AND source_target=?',(KIND,page)).fetchone()[0]==0
        assert copy.execute('SELECT last_cursor_val FROM crawler_sources WHERE source_kind=? AND source_target=?',(KIND,release)).fetchone()
    assert store.get(page)=={'body':'disposable'}
    assert store.get(release)=={'status':'resolved','proof':'keep'}


def test_failed_export_removes_stale_upload_and_keeps_dirty_progress(env,tmp_path,monkeypatch):
    db,path=env;root=tmp_path/'exports';root.mkdir();monkeypatch.setattr(staged,'ROOT',root)
    target=root/'upload/xiaomi.sqlite.enc';target.parent.mkdir();target.write_bytes(b'old snapshot')
    staged.checkpoint_stamp(path,dirty=True)
    monkeypatch.setattr(staged,'seal',lambda *a:(_ for _ in ()).throw(ValueError('Oversize snapshot')))
    with pytest.raises(ValueError):staged.encrypt_file('xiaomi',path)
    assert not target.exists()
    assert json.loads(Path(path).with_suffix('.checkpoint.json').read_text())['dirty'] is True


def test_unexported_local_progress_cannot_be_replaced_by_old_uploaded_artifact(env,monkeypatch):
    _,path=env;staged.checkpoint_stamp(path,artifact_id=9,dirty=True)
    monkeypatch.setenv('FMR_CATALOGUE_LOCAL_STATE','true');artifacts=staged.Artifacts()
    monkeypatch.setattr(artifacts,'list',lambda *a:pytest.fail('Dirty local progress must remain authoritative'))
    try:assert artifacts.restore('xiaomi',path)=='local'
    finally:artifacts.close()
