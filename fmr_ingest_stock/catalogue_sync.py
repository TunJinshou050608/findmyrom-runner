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

DIRECT_BRANDS={'xiaomi','google','samsung','oneplus','motorola','sony'}


def build_version(value):
    raw=str(value).strip()
    if not raw:raise ValueError('No build identity')
    safe=re.sub('[^A-Za-z0-9._-]','_',raw)
    if safe!=raw or len(safe)>64:
        safe=safe[:49]+'-'+hashlib.sha256(raw.encode()).hexdigest()[:12]
    return safe


def package_kind(vendor,release):
    if vendor=='sony' and release.get('kind')=='sony_bundle':return 'sony_bundle'
    path=urlsplit(release['download_url']).path.lower()
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
    brand=vendor if vendor in DIRECT_BRANDS else 'other'
    notes='Partition information is provisional until matching validated standalone images are available.'
    if vendor=='samsung':notes='OEM packages can require FUS authentication/decryption and separate AP/TAR handling. No standalone image compatibility is implied by this listing.'
    if vendor=='motorola':notes='Source provenance distinguishes manufacturer downloads from public archives of stock packages. A listing does not imply a successful device flash.'
    db.execute('''INSERT OR IGNORE INTO devices(codename,brand_id,marketing_name,model_numbers_json,flashing_notes_md,boot_partition_type)
        VALUES (?,?,?,?,?,?)''',(code,brand,device.get('name') or device['upstream_id'],
        json.dumps([v.strip() for v in str(device.get('models') or '').split(',') if v.strip()]),notes or None,
        'boot' if int(device.get('android_release') or 13)<13 else 'init_boot'))
    return code


def publish_release(db,device,release):
    if not release.get('download_url'):return {'status':'unresolved_download'}
    url=release['download_url']
    if device['vendor']=='sony' and release.get('kind')=='sony_bundle':
        from .sony_source import validate_manifest,manifest_digest
        validate_manifest(release['bundle']);bundle_hash=manifest_digest(release['bundle'])
        if url!='https://findmyrom.com/sony/bundles/'+bundle_hash:raise ValueError('Invalid Sony bundle URL')
        origin='sony_assembled_bundle'
    else:origin=download_origin(device['vendor'],url)
    if not origin:return {'status':'untrusted_download_origin'}
    # A rotating signed link cannot be advertised as a permanent download.
    keys={k.lower() for k in __import__('urllib.parse',fromlist=['parse_qs']).parse_qs(urlsplit(url).query)}
    if keys & {'x-amz-signature','x-goog-signature','expires','token','signature'}:
        return {'status':'requires_download_refresh'}
    code=publish_device(db,device)
    vendor=device['vendor']
    raw_version=str(release['version'])
    if vendor=='google':
        match=re.search(r'\(([A-Za-z][A-Za-z0-9._-]+)(?:[,\s)]|$)',raw_version)
        if match:raw_version=match[1]
    version=build_version(raw_version)
    region=str(release.get('region') or 'Global').strip()[:64]
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
    row=db.query('SELECT public_token,extract_status FROM stock_firmwares WHERE device_codename=? AND region_code=? AND version_str=? AND package_type=?',
                 (code,region,version,kind))[0]
    if device['vendor']=='sony' and release.get('kind')=='sony_bundle':
        db.execute('''INSERT OR IGNORE INTO crawler_sources(device_codename,source_kind,source_target,last_cursor_val,last_checked_at)
            VALUES ('__global__','sony_bundle',?,?,strftime('%s','now'))''',(bundle_hash,json.dumps({'manifest':release['bundle'],'public_token':row['public_token']},sort_keys=True,separators=(',',':'))))
    return {'status':'published_new' if row['public_token']==token else 'preserved_existing',
            'public_token':row['public_token'],'codename':code,'package_type':kind,
            'origin_kind':origin,'image_status':'not_verified'}
