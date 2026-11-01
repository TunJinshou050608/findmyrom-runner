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
        self.cursors={}
        self._counts=None

    def key(self,stage,identity=''):
        return self.prefix+stage+(':'+digest(identity) if identity!='' else '')

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
        for offset in range(0,len(items),25):
            chunk=items[offset:offset+25]
            keys=[key for key,_ in chunk if any(key.startswith(self.prefix+stage+':') for stage in ('model','release'))]
            old=self.get_many(keys) if keys else {}
            counts=json.loads(json.dumps(self._load_counts())) if keys else None
            statements=[];counts_changed=False
            for key,value in chunk:
                body=json.dumps(value,sort_keys=True,separators=(',',':'))
                if not key.startswith(self.prefix) or len(body.encode())>1500000:raise ValueError('Invalid catalogue checkpoint')
                if key in old and old[key]==value:continue
                statements.append((SQL,(KIND,key,body,int(time.time()))))
                if key in keys:
                    stage='models' if key.startswith(self.prefix+'model:') else 'releases'
                    before=old.get(key);prior=before.get('status','pending') if before else None
                    current=value.get('status','pending')
                    if prior!=current:
                        if prior:
                            counts[stage][prior]=max(0,counts[stage].get(prior,0)-1)
                            if not counts[stage][prior]:counts[stage].pop(prior)
                        counts[stage][current]=counts[stage].get(current,0)+1;counts_changed=True
                    was_ready=bool(stage=='releases' and before and prior=='resolved' and not before.get('published'))
                    ready=bool(stage=='releases' and current=='resolved' and not value.get('published'))
                    if was_ready!=ready:
                        counts['ready']=max(0,counts.get('ready',0)+int(ready)-int(was_ready));counts_changed=True
                    old[key]=value
            if counts_changed:
                statements.append((SQL,(KIND,self.key('counts'),json.dumps(counts,sort_keys=True),int(time.time()))))
            if statements:self.db.execute_batch(statements)
            if counts is not None:self._counts=counts

    def put(self,key,value):
        self.put_many([(key,value)])

    def rows(self,stage):
        return [(r['source_target'],json.loads(r['last_cursor_val'])) for r in self.db.query(
            "SELECT source_target,last_cursor_val FROM crawler_sources WHERE device_codename='__global__' AND source_kind=? AND source_target GLOB ? ORDER BY source_target",
            (KIND,self.prefix+stage+':*'))]

    def _load_counts(self):
        if self._counts is not None:return self._counts
        cached=self.get(self.key('counts'))
        if cached is not None:
            self._counts=cached;return cached
        rows=self.db.query("""SELECT CASE WHEN source_target GLOB ? THEN 'models' ELSE 'releases' END AS stage,
            COALESCE(json_extract(last_cursor_val,'$.status'),'pending') AS status,count(*) AS n,
            SUM(CASE WHEN json_extract(last_cursor_val,'$.status')='resolved' AND COALESCE(json_extract(last_cursor_val,'$.published'),0)=0 THEN 1 ELSE 0 END) ready
            FROM crawler_sources WHERE device_codename='__global__' AND source_kind=?
            AND (source_target GLOB ? OR source_target GLOB ?) GROUP BY stage,status""",
            (self.prefix+'model:*',KIND,self.prefix+'model:*',self.prefix+'release:*'))
        result={'models':{},'releases':{},'ready':0}
        for r in rows:
            result[r['stage']][r['status']]=r['n']
            if r['stage']=='releases':result['ready']+=r['ready'] or 0
        self.db.execute(SQL,(KIND,self.key('counts'),json.dumps(result,sort_keys=True),int(time.time())))
        self._counts=result
        return result

    def counts(self):
        if self._counts is not None:
            latest=self.get(self.key('counts'))
            if latest is not None:self._counts=latest
        result=self._load_counts()
        return json.loads(json.dumps({k:result[k] for k in ('models','releases')}))

    @property
    def ready_count(self):return self._load_counts().get('ready',0)

    def advance(self,stage,key):self.cursors[stage]=key

    def ready(self,limit=250):
        if not self.ready_count:return []
        return self._select('release',"json_extract(last_cursor_val,'$.status')='resolved' AND COALESCE(json_extract(last_cursor_val,'$.published'),0)=0",[],limit,'publish')

    def _select(self,stage,condition,args,limit,cursor_name=None):
        name=cursor_name or stage;cursor=self.cursors.get(name,'')
        sql="SELECT source_target,last_cursor_val FROM crawler_sources WHERE device_codename='__global__' AND source_kind=? AND source_target GLOB ? AND source_target>? AND "+condition+" ORDER BY source_target LIMIT ?"
        def select(after):
            return [(r['source_target'],json.loads(r['last_cursor_val'])) for r in self.db.query(sql,[KIND,self.prefix+stage+':*',after,*args,limit])]
        rows=select(cursor)
        if not rows and cursor:
            self.cursors[name]='';rows=select('')
        return rows

    def due(self,stage,cycle,limit,now=None,operation='incremental',publish=False):
        now=int(time.time()) if now is None else now
        if stage=='model':
            condition="(COALESCE(json_extract(last_cursor_val,'$.next_check'),0)<=? OR ?='refresh_sources' OR (?='retry_failed' AND json_extract(last_cursor_val,'$.status') IN ('failed','blocked')))"
            args=[now,operation,operation]
        else:
            condition="(COALESCE(json_extract(last_cursor_val,'$.next_check'),0)<=? OR (?='retry_failed' AND json_extract(last_cursor_val,'$.status') IN ('failed','blocked')) OR (?=1 AND json_extract(last_cursor_val,'$.status')='resolved' AND COALESCE(json_extract(last_cursor_val,'$.published'),0)=0))"
            args=[now,operation,int(publish)]
        condition="COALESCE(json_extract(last_cursor_val,'$.status'),'pending')!='excluded' AND COALESCE(json_extract(last_cursor_val,'$.last_cycle'),'')!=? AND "+condition
        if stage=='release' and self.vendor=='samsung':
            condition+=" AND (COALESCE(json_extract(last_cursor_val,'$.release.resolution'),'')!='requires_samsung_fus' OR COALESCE(json_extract(last_cursor_val,'$.release.download_url'),'')!='')"
        return self._select(stage,condition,[cycle,*args],limit)
