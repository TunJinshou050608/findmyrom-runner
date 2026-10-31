"""Optional, idempotent SP07 projection. Scanner state remains independent."""
import json
import os
import re
from .rule_extractor import safe_url,post_ref


def export_to_custom_resources(db_client):
    from fmr_core.crypto import generate_public_token,sign_custom_resource
    db=db_client;key=os.environ.get('SIGN_PRIVATE_KEY_HEX','')
    if not re.fullmatch(r'[a-f0-9]{64}',key):raise ValueError('Signing key required only for SP07 export')
    result={'inserted':0,'updated':0,'unchanged':0,'skipped':0}
    rows=db.query('SELECT * FROM tg_discovered_packages ORDER BY channel_username,msg_id')
    for row in rows:
        code=row['device_codename'];url=row['original_post_url'] or row['tg_post_url']
        if not code or not safe_url(url) or not db.query('SELECT codename FROM devices WHERE codename=?',(code,)):
            result['skipped']+=1;continue
        try:links=json.loads(row['download_links_json'])
        except (ValueError,TypeError):result['skipped']+=1;continue
        if not links or not all(safe_url(link) for link in links):result['skipped']+=1;continue
        origin=post_ref(url) or (row['channel_username'],row['msg_id'])
        version=row['build_version_tag'] or f'tg-{origin[0]}-{origin[1]}'
        zone='free_rom' if row['resource_kind']=='rom' else 'essential_tool'
        current=db.query('''SELECT public_token,zone_type,category_tag,android_version,maintainer_name,maintainer_contact_tg,published_at,maintainer_opted_out
            FROM custom_resources WHERE device_codename=? AND project_name=? AND build_version_tag=? AND target_post_url=?''',
            (code,row['project_name'],version,url))
        handle=row['maintainer_handle'];contact='https://t.me/'+handle[1:] if handle else None
        values=(zone,row['resource_kind'],row['android_version'],row['maintainer_display_name'] or handle,contact,row['published_timestamp'])
        if current:
            old=current[0]
            if old['maintainer_opted_out'] or values==tuple(old[n] for n in ('zone_type','category_tag','android_version','maintainer_name','maintainer_contact_tg','published_at')):
                result['unchanged']+=1;continue
            signature=sign_custom_resource(old['public_token'],zone,url,key)
            db.execute('''UPDATE custom_resources SET zone_type=?,category_tag=?,android_version=?,maintainer_name=?,maintainer_contact_tg=?,published_at=?,ed25519_signature=? WHERE public_token=?''',values+(signature,old['public_token']))
            result['updated']+=1
        else:
            token=generate_public_token('cr');signature=sign_custom_resource(token,zone,url,key)
            db.execute('''INSERT INTO custom_resources(public_token,device_codename,project_name,build_version_tag,target_post_url,
                zone_type,category_tag,android_version,maintainer_name,maintainer_contact_tg,published_at,ed25519_signature) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
                (token,code,row['project_name'],version,url)+values+(signature,))
            result['inserted']+=1
    return result
