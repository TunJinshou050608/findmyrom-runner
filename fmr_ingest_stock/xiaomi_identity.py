"""Bounded task-side repair. No D1 access; original source evidence is immutable."""
import json
import time
from copy import deepcopy
from .catalogue_store import KIND,digest
from .history import HISTORY_SEMANTICS_REVISION,model_code,official
from .firmware_sync import region_code
from .xiaomi_variants import edition,filename,LABELS


def corrected_release(device,row):
    if not official(row.get('download_url','')):return row
    code=device['upstream_id'];name=filename(row['download_url']);result=deepcopy(row)
    result['region']=region_code(model_code(code,row['version'],name),row['version'])
    variant=edition(code,name)
    if variant:result.update(variant_id=variant,variant_label=LABELS[variant])
    else:result.pop('variant_id',None);result.pop('variant_label',None)
    return result


def repair(store,status,limit=500):
    if getattr(store.db,'mode',None)!='sqlite':raise ValueError('Xiaomi identity repair must stay local')
    marker=store.key('history_identity_repair');state=store.get(marker) or {}
    if state.get('revision')==HISTORY_SEMANTICS_REVISION and state.get('done'):return False
    if state.get('revision')!=HISTORY_SEMANTICS_REVISION:state={'revision':HISTORY_SEMANTICS_REVISION,'cursor':'','repaired':0,'scanned':0}
    rows=store.db.query("SELECT source_target,last_cursor_val FROM crawler_sources WHERE device_codename='__global__' AND source_kind=? AND source_target GLOB ? AND source_target>? ORDER BY source_target LIMIT ?",(KIND,store.prefix+'release:*',state['cursor'],limit))
    from .vendor_catalogue import identity,input_hash
    repaired=0
    for record in rows:
        key=record['source_target'];state['scanned']+=1
        # Read again: an earlier component may already have corrected this target.
        value=store.get(key)
        if value.get('status') in ('excluded','superseded'):
            state['cursor']=key;continue
        desired=corrected_release(value['device'],value['release'])
        target=store.key('release',identity(value['device'],desired))
        if key==target:
            state['cursor']=key;continue
        changes={};visiting=set();moves=[0]
        def read(k):return changes[k] if k in changes else store.get(k)
        def move(k,depth=0):
            v=deepcopy(read(k));row=v['release'];device=v['device']
            if depth>4 or k in visiting:raise ValueError('Cyclic Xiaomi identity repair; retain original records')
            desired=corrected_release(device,row);dest=store.key('release',identity(device,desired))
            if dest==k:return
            visiting.add(k)
            evidence=store.key('identity_evidence',[k,digest(row)])
            if not read(evidence):changes[evidence]={'source_target':k,'record':deepcopy(v)}
            other=deepcopy(read(dest))
            if other and other.get('status') not in ('excluded','superseded'):
                other_dest=store.key('release',identity(other['device'],corrected_release(other['device'],other['release'])))
                if other_dest!=dest:
                    move(dest,depth+1);other=deepcopy(read(dest))
            if not other or other.get('status') in ('excluded','superseded'):
                other={'device':device,'release':desired,'input_hash':input_hash(desired),'status':'pending',
                       'next_check':0,'attempts':0,'published':False,'source_changed':True}
            else:
                old=other['release'];same_file=filename(old.get('download_url',''))==filename(row['download_url'])
                checksum_ok=not (row.get('md5') and old.get('md5') and row['md5']!=old['md5'])
                if not same_file or not checksum_ok:
                    refs=other.setdefault('identity_review_sources',[])
                    if evidence not in refs:refs.append(evidence)
                    other.update(status='blocked',error='LegacyIdentityConflict',next_check=int(time.time())+86400)
            v.update(status='superseded',superseded_by=dest,identity_evidence=evidence,error='ArchiveIdentityCorrected',next_check=2**40)
            changes[k]=v;changes[dest]=other;moves[0]+=1;visiting.remove(k)
        move(key)
        if len(changes)>25:raise ValueError('Xiaomi identity component exceeds atomic budget')
        # Evidence, affected source slots, destinations and counters commit together.
        store.put_many(list(changes.items()))
        repaired+=moves[0];state['repaired']+=moves[0];state['cursor']=key
    state['done']=len(rows)<limit;store.put(marker,state)
    status.log(f"小米旧身份核对：本批 {len(rows)} 条、纠正 {repaired} 条；累计核对 {state['scanned']} 条、纠正 {state['repaired']} 条。原始证据保留，D1 零请求。")
    return not state['done']


def repair_until_deadline(store,status,deadline):
    """Spend only this job's existing local budget; resume on the next job."""
    while repair(store,status,limit=500):
        if time.monotonic()>=deadline:return True
    return False
