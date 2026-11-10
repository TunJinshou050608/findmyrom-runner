"""Persist public metadata without changing immutable download fields."""
import json
from .catalogue_store import digest
from .release_metadata import normalize
from fmr_core.firmware_metadata_schema import ensure


def publish(db, token, device, release):
    ensure(db, apply=getattr(db, 'mode', None) == 'sqlite')
    current = db.query('SELECT android_version,published_date,file_size_str,original_official_url,md5_or_sha256,model_code FROM stock_firmwares WHERE public_token=?', (token,))[0]
    if device['vendor']=='xiaomi':
        from .xiaomi_variants import filename,android_version,canonical_version,edition,label,matches
        from .xiaomi_delivery import missing,available
        actual=filename(current['original_official_url'])
        if actual!=filename(release.get('download_url','')):
            raise ValueError('Metadata archive differs from immutable published archive')
        release={**release,'version':canonical_version(release['version'],actual),'android_version':android_version(actual) or release.get('android_version')}
        model=current.get('model_code') or ''
        variant=model.split('__',1)[1] if '__' in model else edition(device['upstream_id'],actual)
        if variant and matches(device['upstream_id'],actual,variant):
            release.update(variant_id=variant,variant_label=label(variant,device['upstream_id']))
        if missing(release):release.update(availability='unavailable',unavailable_reason='DownloadMirrorUnavailable')
    metadata = normalize(device, release)
    old = db.query('SELECT metadata_json,content_hash FROM firmware_metadata WHERE public_token=?', (token,))
    if old:
        prior = json.loads(old[0]['metadata_json'])
        if device['vendor']=='xiaomi' and prior.get('availability')=='unavailable' and not available(release):
            metadata.update(availability='unavailable',unavailable_reason=prior.get('unavailable_reason'))
        # A temporarily unavailable source is not evidence to erase known values.
        for field in ('android_version','os_name','os_version','published_date','security_patch','original_filename','file_size'):
            if not metadata.get(field) and prior.get(field):
                metadata[field] = prior[field]
        if prior.get('sources',{}).get('official_update') and not metadata['sources'].get('official_update'):
            for field in ('android_version','published_date','security_patch'):
                if prior.get(field):metadata[field]=prior[field]
        metadata['sources'] = {k:metadata['sources'].get(k) or v for k,v in prior.get('sources', {}).items()} | metadata['sources']
        for k,v in prior.get('sources', {}).items():
            if not metadata['sources'].get(k):metadata['sources'][k]=v
    fingerprint = digest(metadata)
    if old and old[0]['content_hash'] == fingerprint:
        return False
    import re
    android = metadata.get('android_version') or (current['android_version'] if re.fullmatch(r'(?:Android\s+)?\d+(?:\.\d+)*',str(current['android_version'] or ''),re.I) else 'unknown')
    if device['vendor']=='nothing' and metadata.get('os_name')=='Nothing OS' and not metadata.get('android_version') and str(current['android_version'])==str(metadata.get('os_version')):
        android='unknown'
    date = metadata.get('published_date') or current['published_date']
    size = metadata.get('file_size') or current['file_size_str']
    body = json.dumps(metadata, sort_keys=True, separators=(',',':'))
    # One atomic batch: failed metadata writes cannot acknowledge a partial update.
    statements = [('INSERT INTO firmware_metadata(public_token,metadata_json,content_hash) VALUES (?,?,?) ON CONFLICT(public_token) DO UPDATE SET metadata_json=excluded.metadata_json,content_hash=excluded.content_hash,updated_at=strftime(\'%s\',\'now\') WHERE firmware_metadata.content_hash!=excluded.content_hash', (token,body,fingerprint))]
    if android != current['android_version'] or date != current['published_date'] or size != current['file_size_str']:
        statements.append(('UPDATE stock_firmwares SET android_version=?,published_date=?,file_size_str=? WHERE public_token=?', (android,date,size,token)))
    db.execute_batch(statements)
    saved = db.query('SELECT content_hash FROM firmware_metadata WHERE public_token=?', (token,))
    if not saved or saved[0]['content_hash'] != fingerprint:
        raise RuntimeError('Metadata update not persisted')
    return True
