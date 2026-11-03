"""Disposable page-cache budgets, isolated from model/release checkpoints."""
from collections import OrderedDict
import json
import time

from .catalogue_store import KIND

PAGE_BYTES = 64 * 1024 * 1024
PAGE_ENTRIES = 2048
MEMORY_BYTES = 8 * 1024 * 1024
MAX_RETENTION = 30 * 86400


class ByteLRU(OrderedDict):
    def __init__(self, limit, weight=len):
        super().__init__();self.limit=limit;self.weight=weight;self.used=0;self.sizes={}

    def __getitem__(self,key):
        value=super().__getitem__(key);self.move_to_end(key);return value

    def get(self,key,default=None):
        try:return self[key]
        except KeyError:return default

    def pop(self,key,default=None):
        if key not in self:return default
        value=super().pop(key);self.used-=self.sizes.pop(key);return value

    def __setitem__(self,key,value):
        self.pop(key);size=self.weight(value)
        if size>self.limit:return
        while self and self.used+size>self.limit:self.pop(next(iter(self)))
        super().__setitem__(key,value);self.sizes[key]=size;self.used+=size

    def clear(self):
        super().clear();self.sizes.clear();self.used=0


class PageCache:
    def __init__(self,store,*,now=time.time,budget=PAGE_BYTES,max_entries=PAGE_ENTRIES):
        self.store=store;self.now=now;self.budget=budget;self.max_entries=max_entries;self.touched=set()
        self.entries={r['source_target']:r for r in store.db.query("""SELECT source_target,
            length(CAST(last_cursor_val AS BLOB)) AS bytes,last_checked_at AS accessed,
            COALESCE(json_extract(last_cursor_val,'$.retained_until'),
                     json_extract(last_cursor_val,'$.checked_at')+?,last_checked_at+?) AS expires
            FROM crawler_sources WHERE device_codename='__global__' AND source_kind=? AND source_target GLOB ?""",
            (MAX_RETENTION,MAX_RETENTION,KIND,store.prefix+'page:*'))}
        for row in self.entries.values():row['accessed']=row['accessed'] or 0
        self.bytes=sum(r['bytes'] for r in self.entries.values());self.evicted=0
        expired=[key for key,row in self.entries.items() if not isinstance(row['expires'],(int,float)) or row['expires']<=now()]
        self.drop(expired)
        self.fit(0,0)

    def drop(self,keys):
        for offset in range(0,len(keys),40):
            chunk=keys[offset:offset+40]
            self.store.db.execute_batch([("DELETE FROM crawler_sources WHERE device_codename='__global__' AND source_kind=? AND source_target=?",(KIND,key)) for key in chunk])
            for key in chunk:
                self.bytes-=self.entries.pop(key)['bytes'];self.evicted+=1;self.touched.discard(key)

    def fit(self,extra_bytes,extra_entries,exclude=None):
        victims=[];size=self.bytes+extra_bytes;count=len(self.entries)+extra_entries
        for key,row in sorted(self.entries.items(),key=lambda item:(item[1]['accessed'],item[0])):
            if size<=self.budget and count<=self.max_entries:break
            if key==exclude:continue
            victims.append(key);size-=row['bytes'];count-=1
        self.drop(victims)

    def get(self,key):
        if key not in self.entries:return None
        row=self.store.get(key)
        if row:self.touch(key)
        return row

    def touch(self,key):
        if key in self.entries:
            self.entries[key]['accessed']=int(self.now());self.touched.add(key)

    def flush(self):
        # One access update per used entry per batch; hits never extend body retention.
        keys=list(self.touched)
        for offset in range(0,len(keys),40):
            self.store.db.execute_batch([("UPDATE crawler_sources SET last_checked_at=? WHERE device_codename='__global__' AND source_kind=? AND source_target=?",(self.entries[key]['accessed'],KIND,key)) for key in keys[offset:offset+40]])
        self.touched.clear()

    def put(self,key,row):
        if not key.startswith(self.store.prefix+'page:'):raise ValueError('Not a page-cache key')
        size=len(json.dumps(row,sort_keys=True,separators=(',',':')).encode())
        if size>self.budget:return
        before=self.entries.get(key,{}).get('bytes',0)
        self.fit(size-before,int(key not in self.entries),exclude=key)
        self.store.put(key,row)
        self.entries[key]={'bytes':size,'accessed':self.now(),'expires':row.get('retained_until',row['checked_at']+MAX_RETENTION)}
        self.bytes+=size-before


def inventory(db):
    return db.query("""SELECT CASE WHEN source_target GLOB 'v2:*:page:*' THEN 'page_cache'
        WHEN source_target GLOB 'v2:*:model:*' THEN 'models'
        WHEN source_target GLOB 'v2:*:release:*' THEN 'releases' ELSE 'other' END AS category,
        COUNT(*) AS records,SUM(length(CAST(last_cursor_val AS BLOB))) AS bytes
        FROM crawler_sources WHERE source_kind=? GROUP BY category""",(KIND,))
