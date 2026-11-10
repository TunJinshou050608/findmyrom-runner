"""Bounded task-side repair. No D1 access; original source evidence is immutable."""
import json
import time
from copy import deepcopy
from .catalogue_store import KIND,digest
from .history import HISTORY_SEMANTICS_REVISION,model_code,official
from .firmware_sync import region_code
from .xiaomi_variants import edition,filename,LABELS
from .xiaomi_devices import physical_code,NAMES,archive_route


def corrected_release(device,row):
    if not official(row.get('download_url','')):return row
    name=filename(row['download_url']);code=physical_code(device['upstream_id'],name);result=deepcopy(row)
    result['region']=region_code(model_code(code,row['version'],name),row['version'])
    variant=edition(code,name)
    if variant:result.update(variant_id=variant,variant_label=LABELS[variant])
    else:result.pop('variant_id',None);result.pop('variant_label',None)
    return result


def corrected_device(store,device,row):
    if not official(row.get('download_url','')):return device
    owner=physical_code(device['upstream_id'],filename(row['download_url']))
    if owner==device['upstream_id']:return device
    saved=store.get(store.key('model',owner))
    if saved:return deepcopy(saved['device'])
    return {'vendor':'xiaomi','upstream_id':owner,'codename':owner,
            'name':NAMES.get(owner,owner),'route_code':archive_route(owner),'aliases':[NAMES.get(owner,owner)]}


def reconcile_conflict(store,key,value):
    """Resolve only evidence-proven aliases/repacked builds; keep every receipt."""
    from .history import conflict_kind,unavailable_full_rom
    from .vendor_catalogue import identity,input_hash
    if value.get('status')!='blocked' or value.get('error')!='LegacyIdentityConflict':return False
    refs=value.get('identity_review_sources',[])
    if len(refs)>16:return False
    candidates=[deepcopy(value['release'])]
    for ref,evidence in store.get_many(refs).items():
        record=evidence.get('record',{})
        if not record.get('release') or not record.get('device'):return False
        row=corrected_release(record['device'],record['release'])
        device=corrected_device(store,record['device'],row)
        if store.key('release',identity(device,row))==key:candidates.append(row)
    available=[r for r in candidates if not unavailable_full_rom(r)]
    original=deepcopy(value);updated=deepcopy(value)
    if not available:
        updated.update(status='unavailable',error='UpstreamNotFullROM',next_check=2**40)
    else:
        from .xiaomi_variants import engineering
        selected=next((r for r in available if not engineering(filename(r['download_url']))),available[0])
        alternates=[]
        for other in available:
            kind,blocking=conflict_kind({'link':selected['download_url'],'md5':selected.get('md5')},
                                       {'link':other['download_url'],'md5':other.get('md5')})
            if blocking:return False
            if kind=='repacked_archive':
                alternates.append({'link':other['download_url'],'md5':other.get('md5'),'size':other.get('size')})
        changed=selected.get('download_url')!=original['release'].get('download_url') or selected.get('md5')!=original['release'].get('md5')
        if changed:selected.pop('_metadata',None)
        if alternates:selected['archive_revisions']=alternates
        updated.update(release=selected,input_hash=input_hash(selected),status='pending',error=None,next_check=0,last_cycle='',attempts=0,
                       published=bool(original.get('published')) and not changed)
    evidence_key=store.key('identity_resolution_evidence',[key,digest(original)])
    store.put_many([(evidence_key,{'source_target':key,'record':original}),(key,updated)])
    return True


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
        target=store.key('release',identity(corrected_device(store,value['device'],value['release']),desired))
        if key==target:
            if reconcile_conflict(store,key,value):state['reconciled_conflicts']=state.get('reconciled_conflicts',0)+1
            state['cursor']=key;continue
        changes={};visiting=set();moves=[0]
        def read(k):return changes[k] if k in changes else store.get(k)
        def move(k,depth=0):
            v=deepcopy(read(k));row=v['release'];device=v['device']
            if depth>4 or k in visiting:raise ValueError('Cyclic Xiaomi identity repair; retain original records')
            desired=corrected_release(device,row);device=corrected_device(store,device,row)
            if device['upstream_id']!=v['device']['upstream_id']:desired.pop('_metadata',None)
            dest=store.key('release',identity(device,desired))
            if dest==k:return
            visiting.add(k)
            evidence=store.key('identity_evidence',[k,digest(row)])
            if not read(evidence):changes[evidence]={'source_target':k,'record':deepcopy(v)}
            other=deepcopy(read(dest))
            if other and other.get('status') not in ('excluded','superseded'):
                other_dest=store.key('release',identity(corrected_device(store,other['device'],other['release']),corrected_release(other['device'],other['release'])))
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


def reconcile_cached_sources(store,status,limit=20):
    """Re-evaluate quarantined source rows from durable parsed evidence, no HTTP."""
    if getattr(store.db,'mode',None)!='sqlite':raise ValueError('Cached Xiaomi repair must stay local')
    from .history import HistoryCheckpoint,history_url,conflict_kind,unavailable_full_rom
    from .xiaomi_variants import engineering
    from .vendor_catalogue import identity,input_hash
    from .catalogue_job import xiaomi_release
    from urllib.parse import unquote,urlsplit
    rows=store.db.query("""SELECT source_target,last_cursor_val FROM crawler_sources
        WHERE device_codename='__global__' AND source_kind=? AND source_target GLOB ?
        AND json_extract(last_cursor_val,'$.status')='blocked'
        AND json_extract(last_cursor_val,'$.error')='SourceIdentityConflict'
        ORDER BY source_target LIMIT ?""",(KIND,store.prefix+'release:*',limit))
    reconciled=0;checkpoint=HistoryCheckpoint(store)
    for record in rows:
        key=record['source_target'];value=json.loads(record['last_cursor_val']);device=value['device'];release=value['release']
        scope=archive_route(device['upstream_id'],device.get('route_code'))
        model=store.get(store.key('model',device['upstream_id'])) or {}
        urls={p['url'] for p in model.get('provenance',{}).get('detail_pages',[])
              if p.get('url') and unquote(urlsplit(p['url']).path).rstrip('/').split('/')[-1]==release['version']}
        for family in ('miui','hyperos'):
            for branch in ('stable','stable beta','weekly','public beta'):
                urls.add(history_url('https://xmfirmwareupdater.com/'+family+'/'+scope+'/'+branch+'/'+release['version']+'/'))
        if len(urls)>20:continue
        saved=checkpoint.read(scope,urls);parsed=[];proof=[]
        for entry in saved.values():
            if entry.get('error'):continue
            proof.append({'url':entry.get('url'),'sha256':entry.get('sha256')})
            for raw in entry.get('rows',[]):
                if raw.get('version')!=release['version'] or not official(raw.get('link','')):continue
                row=deepcopy(raw);owner=physical_code(scope,filename(row['link']))
                row['codename']=model_code(owner,row['version'],filename(row['link']))
                variant=edition(owner,filename(row['link']))
                if variant:row.update(variant_id=variant,variant_label=LABELS[variant])
                else:row.pop('variant_id',None);row.pop('variant_label',None)
                owner_device=corrected_device(store,device,{'download_url':row['link']})
                candidate=xiaomi_release(owner_device,row)
                if candidate['kind']==release['kind']:parsed.append((owner_device,candidate,row))
        if not parsed or len(parsed)>8:continue
        if not any(filename(r['download_url'])==filename(release['download_url']) for _,r,_ in parsed):continue
        groups={}
        for d,r,raw in parsed:groups.setdefault(store.key('release',identity(d,r)),[]).append((d,r,raw))
        changes={};ambiguous=False
        evidence=store.key('identity_resolution_evidence',[key,digest(value)])
        changes[evidence]={'source_target':key,'record':deepcopy(value),'source_pages':proof}
        for target,items in groups.items():
            available=[item for item in items if not unavailable_full_rom(item[2])]
            pool=available or items
            d,r,raw=sorted(pool,key=lambda item:(engineering(filename(item[1]['download_url'])),not bool(item[1].get('md5')),item[1]['download_url']))[0]
            r=deepcopy(r);revisions=[]
            for _,alternate,alternate_raw in available:
                kind,blocking=conflict_kind(raw,alternate_raw)
                if blocking:ambiguous=True;break
                if kind=='repacked_archive':revisions.append(alternate_raw)
            if ambiguous:break
            if revisions:r['archive_revisions']=revisions
            previous=store.get(target)
            if previous and previous.get('status') not in ('excluded','superseded'):
                old=previous['release']
                prior_device=corrected_device(store,previous['device'],old)
                prior_target=store.key('release',identity(prior_device,corrected_release(previous['device'],old)))
                if prior_target!=target:
                    if prior_target not in groups:ambiguous=True;break
                    prior_evidence=store.key('identity_resolution_evidence',[target,digest(previous)])
                    changes[prior_evidence]={'source_target':target,'record':deepcopy(previous)}
                    previous=None
                else:
                    kind,blocking=conflict_kind({'link':old['download_url'],'md5':old.get('md5')},raw)
                    if blocking and available:ambiguous=True;break
                    if target!=key:
                        prior_evidence=store.key('identity_resolution_evidence',[target,digest(previous)])
                        changes[prior_evidence]={'source_target':target,'record':deepcopy(previous)}
            same=bool(previous and previous.get('status') not in ('excluded','superseded') and previous['release'].get('download_url')==r['download_url'] and previous['release'].get('md5')==r.get('md5'))
            updated={'device':d,'release':r,'input_hash':input_hash(r),'status':'pending','error':None,'next_check':0,'attempts':0,'last_cycle':'',
                     'published':bool(previous and previous.get('published') and same)}
            if previous and previous.get('publication'):updated['publication']=deepcopy(previous['publication'])
            if not available:updated.update(status='unavailable',error='UpstreamNotFullROM',next_check=2**40)
            changes[target]=updated
        if ambiguous or len(changes)>24:continue
        if key not in groups:
            original=deepcopy(value);original.update(status='superseded',superseded_by=sorted(groups)[0],identity_evidence=evidence,error='ArchiveIdentityCorrected',next_check=2**40)
            changes[key]=original
        store.put_many(list(changes.items()));reconciled+=1
    if reconciled:
        marker=store.key('history_identity_repair');state=store.get(marker) or {}
        state['source_conflicts_reconciled']=state.get('source_conflicts_reconciled',0)+reconciled
        store.put(marker,state)
        status.log(f'按持久化原始页面重新核验并恢复 {reconciled} 条来源记录；原始凭据保留，未请求网络或 D1。')
    return reconciled


def repair_until_deadline(store,status,deadline):
    """Spend only this job's existing local budget; resume on the next job."""
    reconcile_cached_sources(store,status)
    while repair(store,status,limit=500):
        if time.monotonic()>=deadline:return True
    reconcile_cached_sources(store,status)
    return False
