"""Read existing local catalogue metadata without D1 queries or firmware access."""
import json
import os
from pathlib import Path
import re
import sqlite3
from fmr_ingest_stock.catalogue_store import KIND
from fmr_ingest_stock.device_identity import clean_model_name, marketing_name
from fmr_ingest_stock.oem_sources import canonical_code
from .device_snapshot import (
    SNAPSHOT_FILENAME,
    refresh_or_load_snapshot,
    snapshot_to_catalogue_names,
)

IDENTITY = re.compile(r'[a-z0-9][a-z0-9_-]{2,31}')


def _resolve_v4_staging_dir(root: Path, explicit_directory: str | Path | None) -> Path | None:
    if (root / SNAPSHOT_FILENAME).is_file() or (root / 'collectors').is_dir():
        return root
    if explicit_directory is not None:
        return None
    env_v4 = os.environ.get('FMR_STAGING_V4_DIR')
    if env_v4 and Path(env_v4).is_dir():
        return Path(env_v4)
    sibling = root.parent / 'staging-v4'
    if sibling.is_dir() and ((sibling / 'collectors').is_dir() or (sibling / SNAPSHOT_FILENAME).is_file()):
        return sibling
    default_v4 = Path('/var/lib/findmyrom/staging-v4')
    if default_v4.is_dir() and ((default_v4 / 'collectors').is_dir() or (default_v4 / SNAPSHOT_FILENAME).is_file()):
        return default_v4
    return None


def read_catalogue(directory=None):
    root = Path(directory or os.environ.get('FMR_CATALOGUE_STATE_DIR', '/var/lib/findmyrom/catalogue-v3'))
    v4_dir = _resolve_v4_staging_dir(root, directory)
    if v4_dir is not None:
        snapshot_target = root / SNAPSHOT_FILENAME if (root.is_dir() and os.access(root, os.W_OK)) else (v4_dir / SNAPSHOT_FILENAME)
        snapshot, meta = refresh_or_load_snapshot(v4_dir, snapshot_path=snapshot_target)
        names, sources = snapshot_to_catalogue_names(snapshot)
        if names:
            for item in sources:
                item['snapshot_age_seconds'] = meta['snapshot_age_seconds']
                if meta.get('fallback_used'):
                    item['fallback_used'] = True
            return names, sources

    names = {}
    sources = []
    for path in sorted(root.glob('*.sqlite')):
        # Read a consistent SQLite snapshot, including committed WAL transactions.
        with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=15) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            count = 0
            has_models = False
            if 'collector_devices' in tables:
                has_models = True
                for raw_code, brand_id, mkt_name, models_json in db.execute(
                    'SELECT codename, brand_id, marketing_name, model_numbers_json FROM collector_devices'
                ):
                    vendor = str(brand_id or path.stem).lower()
                    if vendor in ('huawei', 'honor') or 'chromebook' in str(mkt_name or '').lower():
                        continue
                    code = str(raw_code or '').strip().lower()
                    if code == '__global__':
                        continue
                    if not IDENTITY.fullmatch(code):
                        code = canonical_code(vendor, code)
                    if not IDENTITY.fullmatch(code) or code == '__global__':
                        continue
                    try:
                        models = json.loads(models_json or '[]')
                    except Exception:
                        models = []
                    device_obj = {'vendor': vendor, 'codename': code, 'upstream_id': code, 'name': mkt_name, 'models': models}
                    labels = [marketing_name(device_obj), mkt_name, *(models if isinstance(models, list) else [str(models)])]
                    items = names.setdefault(code, [])
                    for label in labels:
                        label = clean_model_name(label) if label else ''
                        for part in [label, *re.split(r'\s*/\s*|\s+aka\s+', label, flags=re.I)]:
                            if part and part.lower() != code and part not in items:
                                items.append(part)
                    count += 1
            if 'crawler_sources' in tables:
                has_models = db.execute("SELECT 1 FROM crawler_sources WHERE source_kind=? AND source_target GLOB 'v2:*:model:*' LIMIT 1", (KIND,)).fetchone() is not None
                rows = db.execute("""SELECT json_extract(last_cursor_val,'$.device')
                    FROM crawler_sources WHERE device_codename='__global__' AND source_kind=?
                    AND source_target GLOB 'v2:*:model:*'
                    AND COALESCE(json_extract(last_cursor_val,'$.status'),'')!='excluded'
                    AND COALESCE(json_extract(last_cursor_val,'$.device.catalogue_scope'),'')!='non_android'""", (KIND,))
                for (raw,) in rows:
                    if raw is None:
                        continue
                    device = json.loads(raw)
                    vendor = device['vendor']
                    code = device.get('canonical_code') or canonical_code(vendor, device.get('codename') or device['upstream_id'])
                    if not IDENTITY.fullmatch(code):
                        continue
                    labels = [marketing_name(device), device.get('codename'), device.get('upstream_id')]
                    models = device.get('models') or ''
                    labels.extend(models if isinstance(models, list) else str(models).split(','))
                    items = names.setdefault(code, [])
                    for label in labels:
                        label = clean_model_name(label) if label else ''
                        for part in [label, *re.split(r'\s*/\s*|\s+aka\s+',label,flags=re.I)]:
                            if part and part.lower() != code and part not in items:
                                items.append(part)
                    count += 1
            # Published device metadata is also usable when a snapshot has no model frontier.
            if 'devices' in tables and not has_models:
                for code, label, models in db.execute('SELECT codename,marketing_name,model_numbers_json FROM devices'):
                    if not IDENTITY.fullmatch(code) or code in names:
                        continue
                    names[code] = list(dict.fromkeys([label, *json.loads(models or '[]')]))
                    count += 1
            sources.append({'file': path.name, 'devices_read': count})
    if not names:
        raise RuntimeError('Local device catalogue is missing or empty; no manual target fallback')
    return names, sources


def select_targets(names, last=None, limit=8):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('Invalid device batch size')
    ordered = sorted(names)
    if last is not None:
        ordered = [code for code in ordered if code > last] + [code for code in ordered if code <= last]
    return ordered[:limit]
