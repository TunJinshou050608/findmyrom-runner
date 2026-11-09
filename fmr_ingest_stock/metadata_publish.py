"""Persist public metadata without changing immutable download fields."""
import json
from .catalogue_store import digest
from .release_metadata import normalize
from fmr_core.firmware_metadata_schema import ensure


def publish(db, token, device, release):
    ensure(db, apply=getattr(db, 'mode', None) == 'sqlite')
    metadata = normalize(device, release)
    old = db.query('SELECT metadata_json,content_hash FROM firmware_metadata WHERE public_token=?', (token,))
    if old:
        prior = json.loads(old[0]['metadata_json'])
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
    current = db.query('SELECT android_version,published_date,file_size_str FROM stock_firmwares WHERE public_token=?', (token,))[0]
    import re
    android = metadata.get('android_version') or (current['android_version'] if re.fullmatch(r'(?:Android\s+)?\d+(?:\.\d+)*',str(current['android_version'] or ''),re.I) else 'unknown')
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
