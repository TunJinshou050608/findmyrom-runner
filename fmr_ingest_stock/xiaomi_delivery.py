"""Bounded header observations, never firmware bodies or D1."""
import hashlib
import json
import os
from pathlib import Path
from functools import lru_cache

@lru_cache(maxsize=1)
def _cache(path,mtime,size):
    if size>131072:return {}
    try:return json.loads(Path(path).read_text())
    except (OSError,ValueError):return {}

def observation(row):
    url=row.get('download_url') or row.get('link') or ''
    path=Path(os.environ.get('FMR_CATALOGUE_STATE_DIR','catalogue-state'))/'xiaomi-source-heads.json'
    try:
        stat=path.stat()
        cache=_cache(str(path),stat.st_mtime_ns,stat.st_size)
    except OSError:return None
    item=cache.get(hashlib.sha256(url.encode()).hexdigest(),{})
    head=item.get('head',{})
    return head if 'original_status' in head else None

def missing(row):
    head=observation(row) or {}
    return head.get('status') in (404,410)

def available(row):
    head=observation(row) or {}
    try:size=int(head.get('length') or 0)
    except (TypeError,ValueError):return False
    return head.get('status')==200 and size>4096 and not any(t in str(head.get('type') or '').lower() for t in ('html','xml','json'))
