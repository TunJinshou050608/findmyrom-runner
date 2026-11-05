"""Merge parallel drain ledgers into the shared publisher checkpoint.

Parallel workers keep isolated publisher.sqlite ledgers so they never overwrite
one another. After the workers stop (including on a quota or transport error),
this step folds every shard ledger into the host's main publisher ledger and
exports a fresh encrypted checkpoint for the next run to resume from.
"""
import json
import sqlite3
from pathlib import Path

from .catalogue_publish import open_ledger
from .staged_catalogue import ROOT, encrypt_file


def merge(root):
    root = Path(root)
    main = root / 'publisher.sqlite'
    ledger = open_ledger(main)
    ledger.close()
    shards = sorted(set(root.glob('parallel-*/publisher.sqlite')) | set(root.glob('shards/*/parallel-*/publisher.sqlite')))
    merged = 0
    used = 0
    for shard in shards:
        try:
            with sqlite3.connect(main) as conn:
                conn.execute('ATTACH DATABASE ? AS shard', (str(shard),))
                before = conn.total_changes
                conn.execute('INSERT OR REPLACE INTO published SELECT vendor,key,hash FROM shard.published')
                merged += conn.total_changes - before
                conn.execute('DETACH DATABASE shard')
                conn.commit()
            used += 1
        except sqlite3.Error as error:
            print(json.dumps({'event': 'shard_merge_skipped', 'path': str(shard), 'error': type(error).__name__}), flush=True)
    with sqlite3.connect(main) as conn:
        total = conn.execute('SELECT count(*) FROM published').fetchone()[0]
    encrypt_file('publisher', main)
    return {'status': 'merged', 'shards_seen': len(shards), 'shards_merged': used, 'rows_merged': merged, 'published_rows': total}


def main():
    result = merge(ROOT)
    print(json.dumps({'event': 'shard_checkpoint_merged', **result}, ensure_ascii=False), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
