"""Private, persistent catalogue state shared by independent vendor workflows."""
import hashlib
import json
import time

KIND = 'oem_incremental_v2'
SQL = '''INSERT INTO crawler_sources(device_codename,source_kind,source_target,last_cursor_val,last_checked_at)
VALUES ('__global__',?,?,?,?) ON CONFLICT(device_codename,source_kind,source_target)
DO UPDATE SET last_cursor_val=excluded.last_cursor_val,last_checked_at=excluded.last_checked_at'''


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


class Store:
    def __init__(self,db,vendor):
        self.db,self.vendor=db,vendor
        self.prefix='v2:'+vendor+':'

    def key(self,stage,identity=''):
        return self.prefix+stage+(':'+digest(identity) if identity else '')

    def get(self,key):
        rows=self.db.query("SELECT last_cursor_val FROM crawler_sources WHERE device_codename='__global__' AND source_kind=? AND source_target=?",(KIND,key))
        return json.loads(rows[0]['last_cursor_val']) if rows else None

    def get_many(self,keys):
        result={}
        for offset in range(0,len(keys),25):
            chunk=keys[offset:offset+25]
            marks=','.join('?' for _ in chunk)
            for row in self.db.query("SELECT source_target,last_cursor_val FROM crawler_sources WHERE device_codename='__global__' AND source_kind=? AND source_target IN ("+marks+")",[KIND,*chunk]):
                result[row['source_target']]=json.loads(row['last_cursor_val'])
        return result

    def put_many(self,items):
        statements=[]
        for key,value in items:
            body=json.dumps(value,sort_keys=True,separators=(',',':'))
            if not key.startswith(self.prefix) or len(body.encode())>1500000:
                raise ValueError('Invalid catalogue checkpoint')
            statements.append((SQL,(KIND,key,body,int(time.time()))))
        for offset in range(0,len(statements),25):
            self.db.execute_batch(statements[offset:offset+25])

    def put(self,key,value):
        self.put_many([(key,value)])

    def rows(self,stage):
        return [(r['source_target'],json.loads(r['last_cursor_val'])) for r in self.db.query(
            "SELECT source_target,last_cursor_val FROM crawler_sources WHERE device_codename='__global__' AND source_kind=? AND source_target GLOB ? ORDER BY source_target",
            (KIND,self.prefix+stage+':*'))]

    def counts(self):
        rows=self.db.query('''SELECT CASE WHEN source_target GLOB ? THEN 'models' ELSE 'releases' END AS stage,
            COALESCE(json_extract(last_cursor_val,'$.status'),'pending') AS status,count(*) AS n
            FROM crawler_sources WHERE device_codename='__global__' AND source_kind=?
            AND (source_target GLOB ? OR source_target GLOB ?) GROUP BY stage,status''',
            (self.prefix+'model:*',KIND,self.prefix+'model:*',self.prefix+'release:*'))
        result={'models':{},'releases':{}}
        for r in rows:result[r['stage']][r['status']]=r['n']
        return result

    def due(self,stage,cycle,limit,now=None,operation='incremental',publish=False):
        now=int(time.time()) if now is None else now
        if stage=='model':
            condition="(COALESCE(json_extract(last_cursor_val,'$.next_check'),0)<=? OR ?='refresh_sources' OR (?='retry_failed' AND json_extract(last_cursor_val,'$.status') IN ('failed','blocked')))"
            args=[now,operation,operation]
        else:
            condition="(COALESCE(json_extract(last_cursor_val,'$.next_check'),0)<=? OR (?='retry_failed' AND json_extract(last_cursor_val,'$.status') IN ('failed','blocked')) OR (?=1 AND json_extract(last_cursor_val,'$.status')='resolved' AND COALESCE(json_extract(last_cursor_val,'$.published'),0)=0))"
            args=[now,operation,int(publish)]
        sql="SELECT source_target,last_cursor_val FROM crawler_sources WHERE device_codename='__global__' AND source_kind=? AND source_target GLOB ? AND COALESCE(json_extract(last_cursor_val,'$.last_cycle'),'')!=? AND "+condition+" ORDER BY COALESCE(json_extract(last_cursor_val,'$.next_check'),0),source_target LIMIT ?"
        params=[KIND,self.prefix+stage+':*',cycle,*args,limit]
        return [(r['source_target'],json.loads(r['last_cursor_val'])) for r in self.db.query(sql,params)]
