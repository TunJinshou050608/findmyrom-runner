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
from .common import private_key
from .device_sync import ensure_base_brands_and_anchor
from .cdn_rewriter import rewrite_to_fast_cdn
from .oem_sources import canonical_code,download_origin

DIRECT_BRANDS={'xiaomi','google','samsung','oneplus','motorola','sony','oppo','vivo','realme','asus','nothing'}


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
    db.execute('''INSERT OR IGNORE INTO devices(codename,brand_id,marketing_name,model_numbers_json,flashing_notes_md,boot_partition_type)
        VALUES (?,?,?,?,?,?)''',(code,brand,name,
        json.dumps([v.strip() for v in models.split(',') if v.strip()]),notes or None,
        'boot' if int(device.get('android_release') or 13)<13 else 'init_boot'))
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
    code=publish_device(db,device)
    vendor=device['vendor']
    raw_version=str(release['version'])
    if vendor=='google':
        match=re.search(r'\(([A-Za-z][A-Za-z0-9._-]+)(?:[,\s)]|$)',raw_version)
        if match:raw_version=match[1]
    version=build_version(raw_version)
    region=str(release.get('region') or 'Global').strip()[:64]
    if vendor=='google' and region=='Global':
        # Official Google labels include carrier/market exceptions after the
        # build and date. Keep them visible instead of silently calling all Global.
        block=re.search(r'\(([^)]+)\)',str(release['version']))
        if block:
            parts=[v.strip() for v in block[1].split(',')[1:]]
            editions=[v for v in parts if v and not re.fullmatch(r'(?:[A-Za-z]{3,9}\.?\s+\d{4}|\d{4}-\d{2}(?:-\d{2})?)',v)]
            if editions:region=' · '.join(editions)[:64]
    kind=package_kind(vendor,release)
    fast=rewrite_to_fast_cdn(vendor,url)
    digest=release.get('sha256') or release.get('md5')
    if digest and not re.fullmatch('[0-9a-fA-F]{32}|[0-9a-fA-F]{64}',digest):raise ValueError('Invalid source checksum')
    token=generate_public_token('fw')
    signature=sign_firmware(token,fast,private_key())
    db.execute('''INSERT OR IGNORE INTO stock_firmwares(public_token,device_codename,region_code,model_code,
        version_str,android_version,package_type,file_size_str,md5_or_sha256,original_official_url,fast_cdn_url,
        published_date,ed25519_signature,extract_status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'skipped')''',
        (token,code,region,str(device.get('codename') or device['upstream_id']),version,
         (re.match(r'^\d+(?:\.\d+)*',str(release.get('android_version') or ''))[0] if re.match(r'^\d+(?:\.\d+)*',str(release.get('android_version') or '')) else str(release.get('android_version') or 'unknown')),kind,release.get('size'),digest.lower() if digest else None,
         url,fast,release.get('date'),signature))
    row=db.query('SELECT public_token,extract_status,original_official_url,fast_cdn_url FROM stock_firmwares WHERE device_codename=? AND region_code=? AND version_str=? AND package_type=?',
                 (code,region,version,kind))[0]
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
    return {'status':'published_new' if row['public_token']==token else 'preserved_existing',
            'public_token':row['public_token'],'codename':code,'package_type':kind,
            'origin_kind':origin,'image_status':'not_verified'}
