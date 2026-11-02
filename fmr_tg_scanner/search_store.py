"""Durable query frontier, evidence ledger and deduplicated products, independent of D1."""
import hashlib
import json
import time
from .storage import SQLiteDB

SCHEMA='''
CREATE TABLE IF NOT EXISTS tg_search_tasks(
 key TEXT PRIMARY KEY,kind TEXT NOT NULL,device TEXT NOT NULL,payload TEXT NOT NULL,
 priority INTEGER NOT NULL,next_at REAL NOT NULL DEFAULT 0,status TEXT NOT NULL DEFAULT 'ready',
 attempts INTEGER NOT NULL DEFAULT 0,last_error TEXT,updated REAL NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS tg_search_due ON tg_search_tasks(status,next_at,priority,updated);
CREATE TABLE IF NOT EXISTS tg_search_records(
 key TEXT PRIMARY KEY,device TEXT NOT NULL,channel TEXT NOT NULL,msg_id INTEGER NOT NULL,
 accepted INTEGER NOT NULL,fingerprint TEXT NOT NULL,payload TEXT NOT NULL,evidence TEXT NOT NULL,
 classification TEXT NOT NULL,reason TEXT NOT NULL,updated REAL NOT NULL);
CREATE INDEX IF NOT EXISTS tg_search_products ON tg_search_records(accepted,device,fingerprint);
CREATE TABLE IF NOT EXISTS tg_search_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
'''


class SearchStore(SQLiteDB):
    def __init__(self,path):
        super().__init__(path);self.execute_script(SCHEMA)
        # Private-repo workflow concurrency guarantees one writer. A working
        # task in a restored snapshot therefore belongs to an interrupted run.
        self.execute("UPDATE tg_search_tasks SET status='ready' WHERE status='working'")
    def meta(self,key,default=None):
        rows=self.query('SELECT value FROM tg_search_meta WHERE key=?',(key,))
        return json.loads(rows[0]['value']) if rows else default
    def set_meta(self,key,value):
        self.execute('INSERT INTO tg_search_meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,json.dumps(value,sort_keys=True)))
    def enqueue(self,kind,device,payload,priority=50):
        # Pagination lives in payload.state and must not enter the task identity.
        identity={k:v for k,v in payload.items() if k not in ('state','reason','depth')}
        key=hashlib.sha256(json.dumps([kind,device,identity],sort_keys=True).encode()).hexdigest()
        existing=self.query('SELECT priority FROM tg_search_tasks WHERE key=?',(key,))
        if existing:
            if priority<existing[0]['priority']:
                self.execute('UPDATE tg_search_tasks SET priority=?,next_at=MIN(next_at,?) WHERE key=?',(priority,time.time(),key))
            return key
        if self.query('SELECT COUNT(*) AS n FROM tg_search_tasks WHERE device=?',(device,))[0]['n']>=1500:
            self.set_meta('frontier_limit_observed',True)
            return None
        self.execute('INSERT OR IGNORE INTO tg_search_tasks(key,kind,device,payload,priority) VALUES (?,?,?,?,?)',
                     (key,kind,device,json.dumps(payload,sort_keys=True),priority))
        return key
    def due(self,devices):
        ordered=list(devices);last=self.meta('last_scheduled_device')
        if last in ordered:
            index=ordered.index(last)+1;ordered=ordered[index:]+ordered[:index]
        rows=[]
        for device in ordered:
            turn=self.meta('scheduler_turn:'+device,0)
            preferred='updated>0' if turn%3==2 else 'updated=0'
            rows=self.query("SELECT * FROM tg_search_tasks WHERE status='ready' AND next_at<=? AND device=? AND "+preferred+" ORDER BY priority,updated,key LIMIT 1",(time.time(),device))
            if not rows:rows=self.query("SELECT * FROM tg_search_tasks WHERE status='ready' AND next_at<=? AND device=? ORDER BY priority,updated,key LIMIT 1",(time.time(),device))
            if rows:break
        if not rows:return None
        task=rows[0];task['payload']=json.loads(task['payload'])
        self.set_meta('last_scheduled_device',task['device'])
        self.set_meta('scheduler_turn:'+task['device'],turn+1)
        self.execute("UPDATE tg_search_tasks SET status='working' WHERE key=?",(task['key'],))
        return task
    def finish(self,task,*,delay=0,error=None,payload=None):
        self.execute("UPDATE tg_search_tasks SET status='ready',next_at=?,last_error=?,attempts=?,payload=?,updated=? WHERE key=?",
                     (time.time()+delay,error,task['attempts']+1 if error else 0,json.dumps(payload if payload is not None else task['payload'],sort_keys=True),time.time(),task['key']))
    def fingerprint(self,package):
        if package.get('delivery_type')=='telegram_message':
            return hashlib.sha256(json.dumps([package['device_codename'],package['tg_post_url']],sort_keys=True).encode()).hexdigest()
        links=json.loads(package['download_links_json'])
        version=package.get('build_version_tag') or package.get('file_attachment_name') or package.get('package_name') or package['tg_post_url']
        return hashlib.sha256(json.dumps([package['device_codename'],package['resource_kind'],package['project_name'].lower(),version,sorted(links)],sort_keys=True).encode()).hexdigest()
    def migrate_message_delivery(self):
        if self.meta('delivery_contract')=='telegram_message_v1':return
        self.execute('BEGIN IMMEDIATE')
        try:
            for row in self.query('SELECT * FROM tg_search_records'):
                p=json.loads(row['payload']);p.update(delivery_type='telegram_message',target_post_url=p['tg_post_url'],download_links_json='[]')
                self.execute('UPDATE tg_search_records SET payload=?,fingerprint=? WHERE key=?',(json.dumps(p,sort_keys=True,ensure_ascii=False),self.fingerprint(p),row['key']))
                if row['reason'] in ('unresolved_download_reference','ambiguous_multi_device_downloads'):
                    key=self.enqueue('post',p['device_codename'],{'channel':p['channel_username'],'msg_id':p['msg_id'],'depth':0},2)
                    self.execute("UPDATE tg_search_tasks SET next_at=0,status='ready' WHERE key=?",(key,))
            self.set_meta('delivery_contract','telegram_message_v1');self.execute('COMMIT')
        except BaseException:self.execute('ROLLBACK');raise
    def save(self,package,evidence,classification,reason,accepted):
        key=f"{package['device_codename']}|{package['channel_username']}|{package['msg_id']}"
        fingerprint=self.fingerprint(package)
        old=self.query('SELECT payload,accepted,evidence FROM tg_search_records WHERE key=?',(key,))
        payload=json.dumps(package,sort_keys=True,ensure_ascii=False);proof=json.dumps(evidence,sort_keys=True,ensure_ascii=False)
        changed=not old or old[0]['payload']!=payload or old[0]['accepted']!=int(accepted)
        self.execute('''INSERT INTO tg_search_records VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET
         accepted=excluded.accepted,fingerprint=excluded.fingerprint,payload=excluded.payload,evidence=excluded.evidence,
         classification=excluded.classification,reason=excluded.reason,updated=excluded.updated
         WHERE tg_search_records.payload!=excluded.payload OR tg_search_records.accepted!=excluded.accepted''',
         (key,package['device_codename'],package['channel_username'],package['msg_id'],int(accepted),fingerprint,payload,proof,classification,reason,time.time()))
        return changed
