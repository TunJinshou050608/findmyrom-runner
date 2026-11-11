"""Insert-only publishing of resolved stock links; never relabel a portal as a ROM.

The original SP02 YAML interface and schema remain unchanged. OEM-specific
identities, region codes, build syntax and package kinds are normalized here.
Existing records, signatures and extraction state are never overwritten.
"""
import hashlib
import json
import re
from urllib.parse import urlsplit
from fmr_core.crypto import generate_public_token,sign_firmware
from fmr_core.extraction_policy import extraction_eligible
from .common import private_key
from .device_sync import ensure_base_brands_and_anchor
from .cdn_rewriter import rewrite_to_fast_cdn
from .oem_sources import canonical_code,download_origin

class FirmwareIdentityPending(RuntimeError):pass


STOCK_COLUMNS = (
    'public_token,extract_status,original_official_url,fast_cdn_url,'
    'md5_or_sha256,android_version,published_date,file_size_str,model_code'
)



DIRECT_BRANDS={'xiaomi','google','samsung','oneplus','motorola','sony','oppo','vivo','realme','asus','nothing'}


def xiaomi_legacy_rows(db,device,code,region,version,kind,base=None):
    from .xiaomi_variants import model_key,version_key
    root=str(device.get('codename') or device['upstream_id'])
    models=[root]+[root+'_'+suffix for suffix in ('global','eea','in','id','ru','tr','tw','jp','sg')]
    if base:models=[model_key(model,base) for model in models]
    return db.query("SELECT "+STOCK_COLUMNS+" FROM stock_firmwares WHERE device_codename=? AND region_code=? AND version_str=? AND package_type=? AND LOWER(COALESCE(NULLIF(model_code,''),device_codename)) IN ("+','.join('?' for _ in models)+") ORDER BY id LIMIT 12",
                    (code,region,build_version(version_key(version,base)),kind,*[m.lower() for m in models]))


def publish_unavailable(db,device,release,reason):
    from .xiaomi_variants import filename,platform_parts
    if device['vendor']!='xiaomi':raise ValueError('Unsupported unavailable source')
    code=device.get('canonical_code') or device['upstream_id']
    parts=platform_parts(release.get('variant_id'))
    base=parts[0] if parts else release.get('variant_id')
    candidates=xiaomi_legacy_rows(db,device,code,release['region'],release.get('source_version') or release['version'],package_kind('xiaomi',release),base)
    from .metadata_publish import publish
    for row in candidates:
        if filename(row['original_official_url'])==filename(release['download_url']):
            publish(db,row['public_token'],device,{**release,'availability':'unavailable','unavailable_reason':reason},current=row)
    return {'status':'unavailable_recorded'}


def build_version(value):
    raw=str(value).strip()
    if not raw:raise ValueError('No build identity')
    safe=re.sub('[^A-Za-z0-9._-]','_',raw)
    if safe!=raw or len(safe)>64:
        safe=safe[:49]+'-'+hashlib.sha256(raw.encode()).hexdigest()[:12]
    return safe


def package_kind(vendor,release):
    if vendor=='sony' and release.get('kind')=='sony_bundle':return 'sony_bundle'
    if release.get('refresh_source'):
        from .download_refresh import valid_release
        if not valid_release(vendor,release):raise ValueError('Invalid refresh release')
        if vendor=='samsung':return 'samsung_encrypted'
        return 'incremental_zip' if release.get('kind') in ('incremental','delta') else 'recovery_zip'
    path=urlsplit(release['download_url']).path.lower()
    if path.endswith('.ozip'):return 'recovery_ozip'
    if path.endswith(('.enc2','.enc4')):return 'samsung_encrypted'
    if path.endswith(('.tgz','.tar.gz')):return 'fastboot_tgz'
    if not path.endswith('.zip'):raise ValueError('No supported firmware suffix')
    kind=release.get('kind','').lower()
    if kind in ('incremental','delta','incremental update'):return 'incremental_zip'
    if vendor=='samsung':return 'samsung_zip'
    if kind in ('factory','factory image'):return 'factory_zip'
    if kind in ('fastboot','fastboot rom'):return 'fastboot_zip'
    return 'recovery_zip'


def publish_device(db,device):
    vendor=device['vendor']
    code=device.get('canonical_code') or canonical_code(vendor,device.get('codename') or device['upstream_id'])
    from .device_identity import marketing_name
    name=marketing_name(device)
    brand='rog' if vendor=='asus' and re.search(r'\bROG\b',name,re.I) else vendor if vendor in DIRECT_BRANDS else 'other'
    models=str(device.get('models') or '')
    if not models and vendor=='asus':models=','.join(re.findall(r'\b(?:AI\d{4}[A-Z]?|ZS\d{3}[A-Z]{2})\b',str(device.get('name') or ''),re.I))
    notes=None
    params=(code,brand,name,json.dumps([v.strip() for v in models.split(',') if v.strip()]),notes,
            'boot' if int(device.get('android_release') or 13)<13 else 'init_boot')
    cache=getattr(db,'_publication_device_cache',None)
    if cache is not None and cache.get(code)==params:
        return code
    db.execute('''INSERT INTO devices(codename,brand_id,marketing_name,model_numbers_json,flashing_notes_md,boot_partition_type)
        VALUES (?,?,?,?,?,?) ON CONFLICT(codename) DO UPDATE SET
        brand_id=excluded.brand_id,marketing_name=excluded.marketing_name,
        model_numbers_json=CASE WHEN excluded.model_numbers_json='[]' THEN devices.model_numbers_json ELSE excluded.model_numbers_json END
        WHERE devices.brand_id!=excluded.brand_id OR devices.marketing_name!=excluded.marketing_name
        OR (excluded.model_numbers_json!='[]' AND devices.model_numbers_json!=excluded.model_numbers_json)''',params)
    if cache is not None:
        cache[code]=params
    return code


def publish_release(db,device,release):
    if not release.get('download_url'):return {'status':'unresolved_download'}
    url=release['download_url']
    if release.get('refresh_source'):
        from .download_refresh import valid_release
        if not valid_release(device['vendor'],release):raise ValueError('Invalid refresh release')
        origin='oem_on_demand'
    elif device['vendor']=='sony' and release.get('kind')=='sony_bundle':
        from .sony_source import validate_manifest,manifest_digest
        validate_manifest(release['bundle']);bundle_hash=manifest_digest(release['bundle'])
        if url!='https://findmyrom.com/sony/bundles/'+bundle_hash:raise ValueError('Invalid Sony bundle URL')
        origin='sony_assembled_bundle'
    else:origin=download_origin(device['vendor'],url)
    if not origin:return {'status':'untrusted_download_origin'}
    # A rotating signed link cannot be advertised as a permanent download.
    from .download_refresh import expiring
    if expiring(url):
        return {'status':'requires_download_refresh'}
    from fmr_core.firmware_metadata_schema import ensure as ensure_metadata_schema
    ensure_metadata_schema(db,apply=getattr(db,'mode',None)=='sqlite')
    vendor=device['vendor']
    code=device.get('canonical_code') or canonical_code(vendor,device.get('codename') or device['upstream_id'])
    raw_version=str(release['version'])
    region=str(release.get('region') or 'Global').strip()[:64]
    if vendor=='google':
        from .release_metadata import google_identity
        raw_version,region,_=google_identity(device,release)
    if vendor=='xiaomi' and release.get('variant_id'):
        from .xiaomi_variants import version_key
        raw_version=version_key(raw_version,release['variant_id'])
    version=build_version(raw_version)
    kind=package_kind(vendor,release)
    fast=rewrite_to_fast_cdn(vendor,url)
    digest=release.get('sha256') or release.get('md5')
    if digest and not re.fullmatch('[0-9a-fA-F]{32}|[0-9a-fA-F]{64}',digest):raise ValueError('Invalid source checksum')
    model=str(device.get('codename') or device['upstream_id'])
    if vendor=='xiaomi':
        from .history import model_code,official
        from .firmware_sync import region_code
        from .xiaomi_variants import edition,filename,model_key,matches,platform_parts,version_key,canonical_version
        from .xiaomi_devices import physical_code
        expected=region_code(model_code(device['upstream_id'],release['version'],filename(url)),release['version'])
        if physical_code(device['upstream_id'],filename(url))!=device['upstream_id'] or not official(url) or expected!=region or not matches(device['upstream_id'],filename(url),release.get('variant_id')):
            raise FirmwareIdentityPending('Xiaomi archive market or edition disagrees with descriptor; retain pending')
        if release.get('variant_id'):model=model_key(model,release['variant_id'])
    existing=db.query("SELECT "+STOCK_COLUMNS+" FROM stock_firmwares WHERE device_codename=? AND region_code=? AND version_str=? AND package_type=? AND COALESCE(NULLIF(model_code,''),device_codename)=?",
                 (code,region,version,kind,model))
    if vendor=='xiaomi':
        if canonical_version(release['version'],filename(url))!=release['version']:
            raise FirmwareIdentityPending('Xiaomi filename has a different patch build')
        if not existing:
            parts=platform_parts(release.get('variant_id'))
            base=parts[0] if parts else release.get('variant_id')
            candidates=xiaomi_legacy_rows(db,device,code,region,release['version'],kind,base)
            existing=[r for r in candidates if filename(r['original_official_url'])==filename(url)]
            if not existing and not release.get('variant_id'):
                from .xiaomi_delivery import missing
                collisions=[r for r in candidates if canonical_version(release['version'],filename(r['original_official_url']))!=release['version'] or edition(device['upstream_id'],filename(r['original_official_url'])) or missing({'download_url':r['original_official_url']})]
                if collisions:
                    version=build_version(version_key(release['version'],'standard'))
                    model=model_key(str(device.get('codename') or device['upstream_id']),'standard')
                    release={**release,'variant_id':'standard','variant_label':'Standard edition'}
                    existing=db.query("SELECT "+STOCK_COLUMNS+" FROM stock_firmwares WHERE device_codename=? AND region_code=? AND version_str=? AND package_type=? AND COALESCE(NULLIF(model_code,''),device_codename)=?",(code,region,version,kind,model))
    if vendor=='google' and not existing:
        # Match the old caption-derived identity, then require the exact same official file.
        # Never change historical URLs, tokens, region keys or signatures.
        legacy=re.search(r'\(([A-Za-z][A-Za-z0-9._-]+)(?:[,\s)]|$)',str(release['version']))
        block=re.search(r'\(([^)]+)\)',str(release['version']))
        old_region=str(release.get('region') or 'Global')
        if block and old_region=='Global':
            editions=[v.strip() for v in block[1].split(',')[1:] if v.strip() and not re.fullmatch(r'(?:[A-Za-z]{3,9}\.?\s+\d{4}|\d{4}-\d{2}(?:-\d{2})?)',v.strip())]
            if editions:old_region=' · '.join(editions)[:64]
        legacy_rows=db.query("SELECT "+STOCK_COLUMNS+" FROM stock_firmwares WHERE device_codename=? AND region_code=? AND version_str=? AND package_type=? AND COALESCE(NULLIF(model_code,''),device_codename)=?",
            (code,old_region,build_version(legacy[1] if legacy else release['version']),kind,model))
        existing=[r for r in legacy_rows if r['original_official_url']==url]
        if legacy_rows and not existing:raise FirmwareIdentityPending('Google legacy file differs; keep pending')
    if vendor=='google' and existing and existing[0]['original_official_url']!=url:
        raise FirmwareIdentityPending('Google canonical build points to a different official file')
    if vendor=='xiaomi' and existing:
        from urllib.parse import unquote
        old=existing[0]
        old_name=unquote(urlsplit(old['original_official_url']).path).rsplit('/',1)[-1]
        same_file=old_name==unquote(urlsplit(url).path).rsplit('/',1)[-1]
        if not same_file and release.get('archive_revisions'):
            from .history import conflict_kind,official
            for alternate in release['archive_revisions']:
                candidate=alternate.get('link','')
                if not official(candidate) or filename(candidate)!=old_name:continue
                if conflict_kind({'link':url,'md5':digest},alternate)!=('repacked_archive',False):continue
                if old.get('md5_or_sha256') and alternate.get('md5') and old['md5_or_sha256'].lower()!=alternate['md5'].lower():continue
                release={**release,'download_url':candidate,'md5':alternate.get('md5'),'size':alternate.get('size') or release.get('size')}
                release.pop('_metadata',None)
                url=candidate;digest=alternate.get('md5');same_file=True
                break
        checksum_conflict=bool(digest and old.get('md5_or_sha256') and digest.lower()!=old['md5_or_sha256'].lower())
        if not same_file or checksum_conflict:
            raise FirmwareIdentityPending('Xiaomi existing identity points to another file; retain pending for review')
    publish_device(db,device)
    token=None
    if not existing:
        token=generate_public_token('fw')
        signature=sign_firmware(token,fast,private_key())
        status='pending' if extraction_eligible(vendor,kind,fast) else 'skipped'
        version_match=re.match(r'^\d+(?:\.\d+)*',str(release.get('android_version') or ''))
        android=version_match[0] if version_match else str(release.get('android_version') or 'unknown')
        inserted=db.execute('''INSERT OR IGNORE INTO stock_firmwares(public_token,device_codename,region_code,model_code,
            version_str,android_version,package_type,file_size_str,md5_or_sha256,original_official_url,fast_cdn_url,
            published_date,ed25519_signature,extract_status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (token,code,region,model,version,
             android,kind,release.get('size'),digest.lower() if digest else None,
             url,fast,release.get('date'),signature,status))
        if inserted.get('changes')==1:
            existing=[dict(public_token=token,extract_status=status,original_official_url=url,
                           fast_cdn_url=fast,android_version=android,published_date=release.get('date'),
                           file_size_str=release.get('size'),model_code=model)]
    row=db.query("SELECT "+STOCK_COLUMNS+" FROM stock_firmwares WHERE device_codename=? AND region_code=? AND version_str=? AND package_type=? AND COALESCE(NULLIF(model_code,''),device_codename)=?",
                 (code,region,version,kind,model)) if not existing else existing
    if not row:raise FirmwareIdentityPending('Model identity conflict; retain pending metadata without modifying another model')
    row=row[0]
    # Repair only a proven expiring URL for the same build/region/type. Keep the
    # public token and extraction status; re-sign the new stable destination.
    if release.get('refresh_source') and expiring(row['original_official_url']):
        db.execute('''UPDATE stock_firmwares SET original_official_url=?,fast_cdn_url=?,ed25519_signature=?
            WHERE public_token=? AND original_official_url=?''',
            (url,fast,sign_firmware(row['public_token'],fast,private_key()),row['public_token'],row['original_official_url']))
    if release.get('refresh_source'):
        from .download_refresh import fingerprint
        source=release['refresh_source']
        db.execute('''INSERT OR IGNORE INTO crawler_sources(device_codename,source_kind,source_target,last_cursor_val,last_checked_at)
            VALUES ('__global__','stock_download_refresh',?,?,strftime('%s','now'))''',
            (fingerprint(source),json.dumps({'source':source,'public_token':row['public_token']},sort_keys=True,separators=(',',':'))))
    if device['vendor']=='sony' and release.get('kind')=='sony_bundle':
        db.execute('''INSERT OR IGNORE INTO crawler_sources(device_codename,source_kind,source_target,last_cursor_val,last_checked_at)
            VALUES ('__global__','sony_bundle',?,?,strftime('%s','now'))''',(bundle_hash,json.dumps({'manifest':release['bundle'],'public_token':row['public_token']},sort_keys=True,separators=(',',':'))))
    from .metadata_publish import publish as publish_metadata
    metadata_changed=publish_metadata(db,row['public_token'],device,release,current=row)
    return {'metadata_changed':metadata_changed,'status':'published_new' if row['public_token']==token else 'preserved_existing',
            'public_token':row['public_token'],'codename':code,'package_type':kind,
            'origin_kind':origin,'image_status':'not_verified'}
