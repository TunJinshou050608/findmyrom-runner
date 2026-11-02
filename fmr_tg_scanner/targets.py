"""Read existing local catalogue metadata without D1 queries or firmware access."""
import json
import os
from pathlib import Path
import re
import sqlite3
from fmr_ingest_stock.catalogue_store import KIND
from fmr_ingest_stock.device_identity import clean_model_name, marketing_name
from fmr_ingest_stock.oem_sources import canonical_code

IDENTITY = re.compile(r'[a-z0-9_-]{3,32}')


def read_catalogue(directory=None):
    root = Path(directory or os.environ.get('FMR_CATALOGUE_STATE_DIR', '/var/lib/findmyrom/catalogue-v3'))
    names = {}
    sources = []
    for path in sorted(root.glob('*.sqlite')):
        # Read a consistent SQLite snapshot, including committed WAL transactions.
        with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=15) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            count = 0
            has_models = False
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
