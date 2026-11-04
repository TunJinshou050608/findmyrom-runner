"""Full, offline evidence audit and one-cohort D1 publication of existing TG records."""
import argparse
import base64
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
import os
from pathlib import Path
import re
import time
from urllib.parse import urlsplit
import zlib

from .frontier_repair import repair
from .html_parser import parse_tg_channel_page, ChannelUnavailableError
from .rule_extractor import extract_clean_heading, post_ref
from .search_policy import analyse, profile_devices
from .search_store import SearchStore
from .targets import read_catalogue
from .taxonomy import VERSION, apply_access_policy
from .trial import atomic_json

AUDIT_VERSION='tg-full-evidence-audit-v1'
DB_ID='5d710374-0aac-43bf-a998-8935859a26c6'
ROOT_FIELDS={'schema_version','access','resource_type','devices','rom_family','system_base','project','maintenance','project_recognition','versions','build_variants','package_form','release_stage','evidence','publication'}


def message_from_event(event):
    p=event['package']
    return {'text':event['observed_text'],'channel_username':p['channel_username'],'msg_id':p['msg_id'],
            'tg_post_url':p['tg_post_url'],'links':list(dict.fromkeys(re.findall(r'https?://[^\s<>]+',event['observed_text']))),'link_items':[],
            'published_timestamp':p.get('published_timestamp'),'file_attachment_name':p.get('file_attachment_name'),
            'has_tg_attachment':bool(p.get('has_tg_attachment')),
            'sender_author_signature':p.get('sender_author_signature'),
            'has_forward_header':p.get('origin_type')=='native_forward',
            'forwarded_from_url':p.get('original_post_url') if p.get('origin_type')=='native_forward' else None}


def evidence_messages(root,store,rows):
    wanted={}
    for row in rows:
        p=json.loads(row['payload'])
        wanted.setdefault((p['channel_username'],p['msg_id']),set()).add(p['raw_text_hash'])
    messages={};stats=Counter()
    # New local workers commit exact source text atomically with the ledger row.
    # A cleaning snapshot therefore cannot race the subsequent JSONL append.
    for row in rows:
        proof=json.loads(row['evidence']);text=proof.get('observed_text')
        p=json.loads(row['payload']);key=(p['channel_username'],p['msg_id'])
        if isinstance(text,str) and hashlib.sha256(text.encode()).hexdigest() in wanted[key]:
            messages[key]=message_from_event({'package':p,'observed_text':text})
            stats['messages_recovered_from_ledger']+=1
    for path in sorted((root/'search-v3/logs').glob('*.jsonl')):
        for line in path.open():
            try:event=json.loads(line)
            except ValueError:stats['invalid_log_lines']+=1;continue
            if event.get('event')!='resource_decision' or not isinstance(event.get('observed_text'),str):continue
            p=event.get('package',{});key=(p.get('channel_username'),p.get('msg_id'))
            if key not in wanted:continue
            digest=hashlib.sha256(event['observed_text'].encode()).hexdigest()
            if digest in wanted[key]:messages[key]=message_from_event(event)
    stats['messages_recovered_from_logs']=len(messages)
    missing=set(wanted)-set(messages)
    if missing and store.query("SELECT 1 FROM sqlite_master WHERE type='table' AND name='tg_http_cache'"):
        for row in store.connection.execute('SELECT url,body FROM tg_http_cache ORDER BY url'):
            p=urlsplit(row['url']);m=re.fullmatch(r'/(?:s/)?([a-z][a-z0-9_]{3,31})(?:/([0-9]+))?',p.path)
            if p.hostname!='t.me' or not m or not any(key[0]==m[1] for key in missing):continue
            try:
                html=zlib.decompress(row['body']).decode()
                if m[2]:html='<section class="tgme_widget_history">'+html+'</section>'
                for msg in parse_tg_channel_page(html,m[1])['messages']:
                    key=(msg['channel_username'],msg['msg_id'])
                    if key in missing and hashlib.sha256(msg['text'].encode()).hexdigest() in wanted[key]:
                        messages[key]=msg;missing.remove(key);stats['messages_recovered_from_cache']+=1
            except (ValueError,UnicodeError,zlib.error,ChannelUnavailableError):stats['unusable_cache_pages']+=1
            if not missing:break
    stats['missing_source_messages']=len(missing)
    return messages,dict(stats)


def normalize_device(code,names):
    if code in names:return code
    matches=[key for key,labels in names.items() if code.lower() in {v.lower() for v in labels}]
    return matches[0] if len(matches)==1 else None


def enrich_release(package,message):
    """Use release headings/declared fields, never a random changelog number."""
    text=message['text'];heading=extract_clean_heading(text)
    project=package['project_name']
    candidates=[heading]+[line for line in text.splitlines() if re.match(r'(?i)^\s*(?:version|build\s*(?:id|number|version)|版本|构建编号)\s*[:：-]',line)]
    version=None
    for line in candidates:
        m=re.search(r'(?i)\b(?:v|r)\d+(?:\.[\w-]+)*\b',line)
        if not m and project:
            m=re.search(re.escape(project)+r'\s*[-:|]?\s*(\d+(?:\.[\w-]+)*(?:\s+[A-Za-z]+)?)',line,re.I)
            if m:version=m[1].strip();break
        if m:version=m[0];break
    if version is None:
        for line in text.splitlines():
            if re.search(r'(?i)\bdownload\b|kernel\s+zip|版本|构建编号',line):
                m=re.search(r'(?i)\bv\d+(?:\.[\w-]+)*\b',line)
                if m:version=m[0];break
    tax=package['taxonomy'];versions=tax['versions']
    if version:
        package['build_version_tag']=version
        if versions.get('project_version') is None and re.match(r'(?i)^[vr]\d',version):
            versions['project_version']=version[1:]
            tax['evidence']['versions.project_version']={'value':version[1:],'source':'release_heading_or_label','evidence':version}
    else:
        package['build_version_tag']=None
    # Build dates may remain null; expose missing facts explicitly rather than inventing them.
    package['taxonomy']['versions']=versions
    return package


def quality_errors(package,accepted):
    t=package.get('taxonomy',{});errors=[]
    if not accepted:errors.append('not_accepted_by_current_policy')
    if not ROOT_FIELDS<=set(t):errors.append('incomplete_taxonomy_schema');return errors
    if not t['publication']['eligible']:errors.extend(t['publication']['blockers'])
    if t['schema_version']!=VERSION:errors.append('outdated_taxonomy')
    if not post_ref(package.get('tg_post_url','')):errors.append('missing_exact_post_link')
    if not t['devices'].get('device_id') or not t['devices'].get('codename'):errors.append('unresolved_device_identity')
    if not t['project'].get('id') or not t['project'].get('name'):errors.append('missing_project_identity')
    if not t['maintenance'].get('maintainer_id') or not t['maintenance'].get('branch_id'):errors.append('missing_observed_maintainer_or_branch')
    if not package.get('build_version_tag'):errors.append('missing_release_version')
    if not isinstance(package.get('published_timestamp'),(int,float)) or package['published_timestamp']<=0:errors.append('missing_observed_publication_time')
    if t['resource_type']=='rom' and not t['versions'].get('android'):errors.append('missing_android_version')
    for key in ('android','project_version','build_id','build_date'):
        if key not in t['versions']:errors.append('missing_version_field:'+key)
    for key in ('device_id','codename','applicable_models','excluded_models','compatibility_conditions'):
        if key not in t['devices']:errors.append('missing_device_field:'+key)
    return sorted(set(errors))


def audit(root):
    root=Path(root);run_id=os.environ.get('GITHUB_RUN_ID','local');out=root/'audits'/('full-quality-'+run_id)
    out.mkdir(parents=True,exist_ok=True)
    store=SearchStore(root/'search-v3/search.sqlite')
    try:
        retired=repair(store)
        rows=store.query('SELECT * FROM tg_search_records ORDER BY device,channel,msg_id')
        names,sources=read_catalogue()
        if 'alioth' in names:names['alioth']=list(dict.fromkeys([*names['alioth'],'POCO F3','Mi 11X','Redmi K40']))
        saved=store.meta('device_aliases_v3',{})
        for code,labels in saved.items():
            normalized=normalize_device(code,names)
            if normalized:
                names[normalized]=list(dict.fromkeys([*names[normalized],*labels]))
        messages,recovery=evidence_messages(root,store,rows)
        grounded=os.getenv('FMR_TG_GROUNDED_CLEANER')=='true'
        extractions={};model_stats={}
        if grounded:
            from .grounded_cleaner import REVISION as CLEANER_REVISION,extract_all,convert
            items=[]
            for row in rows:
                old=json.loads(row['payload']);code=normalize_device(old['device_codename'],names)
                msg=messages.get((old['channel_username'],old['msg_id']))
                if code and msg and hashlib.sha256(msg['text'].encode()).hexdigest()==old['raw_text_hash']:items.append((code,msg))
            extractions,model_stats=extract_all(items,names,store)
        decisions=[];qualified={};reasons=Counter();counts=Counter()
        for index,row in enumerate(rows):
            old=json.loads(row['payload']);key=(old['channel_username'],old['msg_id'])
            code=normalize_device(old['device_codename'],names)
            message=messages.get(key);errors=[];result=None;reason=None
            if not code:errors.append('device_not_in_existing_catalogue')
            if not message:errors.append('source_text_not_recoverable')
            elif hashlib.sha256(message['text'].encode()).hexdigest()!=old['raw_text_hash']:errors.append('source_text_hash_mismatch')
            if not errors:
                profile=store.meta('profile:'+old['channel_username'],'')
                result,reason=convert(old,message,code,names,extractions.get((code,message['channel_username'],message['msg_id']))) if grounded else analyse(message,code,names[code],names,profile)
                if result is None:errors.append(reason)
                else:
                    package=result['package'] if grounded else enrich_release(result['package'],message)
                    # Factual sender/provenance fields survive only the source-hash check.
                    package['taxonomy']['audit_missing_facts']=[k for k,v in package['taxonomy']['versions'].items() if v is None]
                    errors.extend(quality_errors(package,result['accepted']))
                    if not errors:
                        identity=(code,package['tg_post_url'])
                        qualified[identity]={'package':package,'evidence':result['evidence'],'observed_text':message['text'],
                                             'previous_key':row['key'],'audit_version':AUDIT_VERSION}
                    if errors:
                        package['taxonomy']['publication']={'eligible':False,'blockers':sorted(set(errors))}
                    store.save(package,{**result['evidence'],'audit_version':AUDIT_VERSION,'source_text_hash_verified':True,'observed_text':message['text']},result['classification'],reason if not errors else 'quality:'+errors[0],not errors)
            if (errors and result is None) or (code and code!=old['device_codename']):
                old['taxonomy']=deepcopy(old.get('taxonomy',{}))
                old['taxonomy']['publication']={'eligible':False,'blockers':sorted(set(errors or ['device_identity_remapped']))}
                store.execute('UPDATE tg_search_records SET accepted=0,reason=?,payload=? WHERE key=?',
                              ('quality:'+(errors[0] if errors else 'device_identity_remapped'),json.dumps(old,sort_keys=True,ensure_ascii=False),row['key']))
            counts['previously_accepted' if row['accepted'] else 'previously_candidate']+=1
            for error in set(errors):reasons[error]+=1
            decisions.append({'key':row['key'],'previously_accepted':bool(row['accepted']),'device':code,'post_url':old['tg_post_url'],
                              'qualified':not errors,'reasons':sorted(set(errors))})
            if index%500==0:print(json.dumps({'event':'full_quality_progress','checked':index,'total':len(rows),'qualified':len(qualified)}),flush=True)
        report={'cleaner_revision':CLEANER_REVISION if grounded else 'rules','model_usage':model_stats,'audit_version':AUDIT_VERSION,'audit_run_id':run_id,'all_records_checked':len(rows),'unique_posts_recovered':len(messages),
                'qualified':len(qualified),'rejected_or_incomplete':sum(not d['qualified'] for d in decisions),
                'reason_counts':dict(reasons),'previous_counts':dict(counts),'evidence_recovery':recovery,
                'invalid_channel_tasks_retired':retired,'device_catalogue_count':len(names),'catalogue_sources':sources,
                'firmware_downloaded':False,'new_tg_network_requests':0,'D1_writes':0,
                'completed_at':datetime.now(timezone.utc).isoformat()}
        atomic_json(out/'qualified.json',{'audit_version':AUDIT_VERSION,'products':list(qualified.values())})
        atomic_json(root/'search-v3/publishable_products.json',{'taxonomy_version':VERSION,'audit_version':AUDIT_VERSION,'products':list(qualified.values())})
        atomic_json(root/'search-v3/products.json',{'delivery_type':'telegram_message','audit_version':AUDIT_VERSION,'products':list(qualified.values())})
        atomic_json(out/'decisions.json',{'decisions':decisions})
        atomic_json(out/'report.json',report)
        atomic_json(root/'audits/full-quality-latest.json',report)
        print(json.dumps({'event':'full_quality_finished',**report}),flush=True)
        return out
    finally:store.close()


def resource_token(package):
    digest=hashlib.sha256((package['device_codename']+'|'+package['tg_post_url']).encode()).digest()
    # Existing SSR/verify/download routes accept exactly 10–13 token characters.
    return 'cr_'+base64.urlsafe_b64encode(digest).decode().rstrip('=')[:13]


def publication_row(item,key,run_id):
    from fmr_core.crypto import sign_custom_resource
    p=item['package'];t=p['taxonomy'];url=p['tg_post_url']
    token=resource_token(p)
    zone='free_rom' if t['resource_type']=='rom' else 'essential_tool'
    categories={'aosp_like':'类原生','manufacturer_modified':'厂商修改','manufacturer_port':'厂商移植','other':'其他','pending':'待确认'}
    category=categories.get(t['rom_family'],'待确认') if t['resource_type']=='rom' else {'kernel':'Kernel','recovery':'Recovery','other_tool':'其他工具'}[t['resource_type']]
    metadata={'taxonomy':t,'evidence':item['evidence'],'source_text_hash':p['raw_text_hash'],
              'audit_version':AUDIT_VERSION,'audit_run_id':run_id,'source_post_url':url,
              'project_recognition_is_author_claim':True}
    return (token,p['device_codename'],zone,category,t['project']['name'],int(t['project_recognition']=='official'),
            t['versions']['android'],p['build_version_tag'],p.get('maintainer_display_name') or p.get('maintainer_handle'),
            'https://t.me/'+p['maintainer_handle'].lstrip('@') if p.get('maintainer_handle') else None,url,
            ', '.join(t['build_variants']),json.dumps([],ensure_ascii=False),p['published_timestamp'],
            sign_custom_resource(token,zone,url,key),json.dumps(metadata,sort_keys=True,ensure_ascii=False),run_id)


def publish(out,db,key):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization
    from fmr_core.crypto import verify_signature,build_firmware_payload,build_custom_resource_payload
    out=Path(out);report=json.loads((out/'report.json').read_text());run_id=report['audit_run_id']
    if not re.fullmatch('[a-fA-F0-9]{64}',key):raise ValueError('Valid existing signing key required')
    public=Ed25519PrivateKey.from_private_bytes(bytes.fromhex(key)).public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw).hex()
    existing_fw=db.query('SELECT public_token,fast_cdn_url,ed25519_signature FROM stock_firmwares ORDER BY id DESC LIMIT 10')
    if existing_fw and not any(verify_signature(build_firmware_payload(r['public_token'],r['fast_cdn_url']),r['ed25519_signature'],public) for r in existing_fw):
        raise ValueError('Signing key does not match existing production firmware signatures')
    products=json.loads((out/'qualified.json').read_text())['products']
    def select_in(table,field,values,projection='*'):
        values=list(dict.fromkeys(values));result=[]
        for offset in range(0,len(values),80):
            chunk=values[offset:offset+80]
            result.extend(db.query('SELECT '+projection+' FROM '+table+' WHERE '+field+' IN ('+','.join('?' for _ in chunk)+')',chunk))
        return result
    devices={r['codename'] for r in select_in('devices','codename',[i['package']['device_codename'] for i in products],'codename')}
    candidate_tokens=[]
    for item in products:
        package=item['package']
        candidate_tokens.extend([resource_token(package),'cr_'+hashlib.sha256((package['device_codename']+'|'+package['tg_post_url']).encode()).hexdigest()[:24]])
    existing_rows={r['public_token']:r for r in select_in('custom_resources','public_token',candidate_tokens)}
    opted_out={token for token,row in existing_rows.items() if row['maintainer_opted_out']}
    ready=[];excluded=[]
    for item in products:
        errors=quality_errors(item['package'],True)
        if item['package']['device_codename'] not in devices:errors.append('device_absent_from_production_D1')
        token=resource_token(item['package'])
        legacy_token='cr_'+hashlib.sha256((item['package']['device_codename']+'|'+item['package']['tg_post_url']).encode()).hexdigest()[:24]
        if token in opted_out or legacy_token in opted_out:errors.append('maintainer_opted_out')
        if errors:excluded.append({'post_url':item['package']['tg_post_url'],'device':item['package']['device_codename'],'reasons':errors})
        else:ready.append(item)
    atomic_json(out/'publication_exclusions.json',{'excluded':excluded})
    columns='public_token,device_codename,zone_type,category_tag,project_name,is_official,android_version,build_version_tag,maintainer_name,maintainer_contact_tg,target_post_url,gapps_type,known_bugs_json,published_at,ed25519_signature,metadata_json,audit_batch_id'
    # Repair only this audit publisher's initial overlong deterministic tokens.
    # Retain the actual resource rows/counters; never delete user resources.
    legacy={token:r for token,r in existing_rows.items() if json.loads(r['metadata_json']).get('audit_version')==AUDIT_VERSION}
    updates=','.join(c+'=excluded.'+c for c in columns.split(',') if c!='public_token')
    sql='INSERT INTO custom_resources('+columns+') VALUES ('+','.join('?' for _ in columns.split(','))+') ON CONFLICT(public_token) DO UPDATE SET '+updates+' WHERE custom_resources.maintainer_opted_out=0'
    keep={resource_token(item['package']) for item in ready}
    keep.update('cr_'+hashlib.sha256((item['package']['device_codename']+'|'+item['package']['tg_post_url']).encode()).hexdigest()[:24] for item in ready)
    expected={};committed=0;rows_upserted=0
    for offset in range(0,len(ready),10):
        chunk=ready[offset:offset+10];statements=[]
        for item in chunk:
            values=publication_row(item,key,run_id);expected[values[0]]=values
            p=item['package']
            old='cr_'+hashlib.sha256((p['device_codename']+'|'+p['tg_post_url']).encode()).hexdigest()[:24]
            if old in legacy and legacy[old]['device_codename']==p['device_codename'] and legacy[old]['target_post_url']==p['tg_post_url']:
                statements.append(('UPDATE custom_resources SET public_token=?,ed25519_signature=? WHERE public_token=? AND maintainer_opted_out=0',(values[0],values[14],old)))
            before=existing_rows.get(values[0])
            same=False
            if before:
                pairs=dict(zip(columns.split(','),values))
                old_meta=json.loads(before['metadata_json']);new_meta=json.loads(pairs['metadata_json'])
                old_meta.pop('audit_run_id',None);new_meta.pop('audit_run_id',None)
                same=old_meta==new_meta and all(before[k]==v for k,v in pairs.items() if k not in ('metadata_json','audit_batch_id'))
            if not same:
                statements.append((sql,values));rows_upserted+=1
        # Each request is an atomic chunk of the same audited full publication cohort.
        if statements:db.execute_batch(statements)
        committed+=len(chunk)
        atomic_json(out/'publication_progress.json',{'committed':committed,'planned':len(ready),'audit_run_id':run_id})
        print(json.dumps({'event':'D1_publication_progress','committed':committed,'planned':len(ready)}),flush=True)
    result_rows=[];tokens=list(expected)
    for offset in range(0,len(tokens),40):
        chunk=tokens[offset:offset+40]
        result_rows.extend(db.query('SELECT public_token,zone_type,target_post_url,ed25519_signature,metadata_json,device_codename,maintainer_opted_out FROM custom_resources WHERE public_token IN ('+','.join('?' for _ in chunk)+')',chunk))
    for row in result_rows:
        if row['public_token'] not in expected:raise RuntimeError('Unexpected resource in publication batch')
        if row['maintainer_opted_out']:raise RuntimeError('Resource opted out during publication; retry without republishing it')
        if not verify_signature(build_custom_resource_payload(row['public_token'],row['zone_type'],row['target_post_url']),row['ed25519_signature'],public):raise RuntimeError('Published signature verification failed')
        if json.loads(row['metadata_json'])['taxonomy']['publication']['eligible'] is not True:raise RuntimeError('Published record failed its taxonomy gate')
    if len(result_rows)!=len(ready):raise RuntimeError('Published row count does not match the audited cohort')
    # Withdraw only after every new row is committed and verified. A failed or
    # budget-deferred run must not remove the previous working catalogue first.
    withdrawals=[];after_id=0
    owner="json_extract(CASE WHEN json_valid(metadata_json) THEN metadata_json ELSE '{}' END,'$.audit_version')"
    while True:
        page=db.query('SELECT * FROM custom_resources WHERE '+owner+'=? AND maintainer_opted_out=0 AND id>? ORDER BY id LIMIT 40',(AUDIT_VERSION,after_id))
        if not page:break
        after_id=page[-1]['id']
        withdrawals.extend(r for r in page if r['public_token'] not in keep)
    atomic_json(out/'withdrawn_previous_audit_rows.json',{'reason':'failed corrected full audit','rows':withdrawals})
    for offset in range(0,len(withdrawals),10):
        chunk=withdrawals[offset:offset+10]
        db.execute_batch([("DELETE FROM custom_resources WHERE public_token=? AND maintainer_opted_out=0 AND json_extract(metadata_json,'$.audit_version')=?",(r['public_token'],AUDIT_VERSION)) for r in chunk])
    visible_devices=sorted({r['device_codename'] for r in result_rows if not r['maintainer_opted_out']})
    for offset in range(0,len(visible_devices),50):
        chunk=visible_devices[offset:offset+50]
        db.execute('UPDATE devices SET phase2_roms_enabled=1 WHERE phase2_roms_enabled!=1 AND codename IN ('+','.join('?' for _ in chunk)+')',chunk)
    resources_by_device={}
    for row in result_rows:
        tab='roms' if row['zone_type']=='free_rom' else 'recoveries'
        resources_by_device.setdefault(row['device_codename'],{}).setdefault(tab,[]).append(row['public_token'])
    result={'resources_by_device':resources_by_device,'audit_run_id':run_id,'qualified_before_D1_device_check':len(products),'published_and_verified':len(result_rows),
            'withdrawn_previous_audit_records':len(withdrawals),
            'D1_rows_upserted':rows_upserted,
            'excluded_missing_production_device_or_quality':len(excluded),'enabled_devices':visible_devices,'signatures_verified':True,
            'completed_at':datetime.now(timezone.utc).isoformat(),'retrieval_remains_paused':os.environ.get('FMR_TG_LOCAL_RUNTIME')!='true'}
    atomic_json(out/'publication_result.json',result)
    atomic_json(out.parent/'publication-latest.json',result)
    print(json.dumps({'event':'D1_publication_finished',**result}),flush=True)
    return result


def verify_site(out, client=None, sleeper=time.sleep):
    import httpx
    out=Path(out)
    result=json.loads((out/'publication_result.json').read_text())
    products=json.loads((out/'qualified.json').read_text())['products']
    by_device={}
    for item in products:
        p=item['package'];by_device.setdefault(p['device_codename'],[]).append(item)
    own=client is None
    client=client or httpx.Client(timeout=30,follow_redirects=False,headers={'User-Agent':'Mozilla/5.0 (compatible; FindMyROM publication verifier)'})
    checks=[]
    try:
        for code in result['enabled_devices']:
            tabs=result.get('resources_by_device',{}).get(code)
            if tabs is None:
                tabs={}
                for item in by_device[code]:
                    tab='roms' if item['package']['taxonomy']['resource_type']=='rom' else 'recoveries'
                    tabs.setdefault(tab,[]).append(resource_token(item['package']))
            for tab,tokens in tabs.items():
                expected=set(tokens);url='https://findmyrom.com/devices/'+code+'?tab='+tab
                for attempt in range(2):
                    observed=set()
                    for page in range(1,1001):
                        response=client.get(url+('&rpage='+str(page) if page>1 else ''));response.raise_for_status()
                        observed.update(re.findall(r'/verify/(cr_[A-Za-z0-9_-]{10,13})(?![A-Za-z0-9_-])',response.text))
                        if expected<=observed or 'More projects' not in response.text:break
                    if expected<=observed:break
                    if attempt==0:sleeper(35)
                if not expected<=observed:raise RuntimeError('Published page token set mismatch for '+code)
                if not expected:continue
                sample=sorted(expected)[0]
                response=client.get('https://findmyrom.com/verify/'+sample);response.raise_for_status()
                checks.append({'device':code,'page_url':url,'visible_release_links':len(expected),'sample_verify_page_status':response.status_code})
        report={'verified_resources':sum(c['visible_release_links'] for c in checks),'verified_devices':len({c['device'] for c in checks}),'checks':checks}
        atomic_json(out/'website_verification.json',report)
        print(json.dumps({'event':'website_verification_finished',**report}),flush=True)
        return report
    finally:
        if own:client.close()


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--state-dir',required=True);parser.add_argument('--publish',action='store_true');parser.add_argument('--publish-only',action='store_true');parser.add_argument('--verify-site',action='store_true');args=parser.parse_args()
    root=Path(args.state_dir)
    if args.verify_site:
        verify_site(root/'audits'/('full-quality-'+os.environ.get('GITHUB_RUN_ID','local')))
        return
    out=root/'audits'/('full-quality-'+os.environ.get('GITHUB_RUN_ID','local')) if args.publish_only else audit(root)
    if args.publish:
        if os.environ.get('CF_D1_DATABASE_ID')!=DB_ID:raise ValueError('Production D1 identity must match the established database')
        from fmr_ingest_stock.d1_budget import BudgetDB
        from fmr_core.schema_migrations import ensure_schema
        db=BudgetDB(cf_database_id=DB_ID)
        try:
            if not db.acquire():return 0
            ensure_schema(db)
            publish(out,db,os.environ.get('SIGN_PRIVATE_KEY_HEX','').strip())
        finally:
            try:db.settle()
            finally:db.close()


if __name__=='__main__':main()
