"""Merge parallel drain ledgers into the shared publisher checkpoint.

Parallel workers keep isolated publisher.sqlite ledgers so they never overwrite
one another. After the workers stop (including on a quota or transport error),
this step folds every shard ledger into the host's main publisher ledger and
exports a fresh encrypted checkpoint for the next run to resume from.
"""
import json
import sqlite3
import time
from pathlib import Path

from .staged_catalogue import ROOT, encrypt_file


def merge(root):
    root = Path(root)
    main = root / 'publisher.sqlite'
    with sqlite3.connect(main, timeout=30) as conn:
        conn.execute('PRAGMA busy_timeout=30000')
        conn.execute('CREATE TABLE IF NOT EXISTS published(vendor TEXT,key TEXT,hash TEXT,PRIMARY KEY(vendor,key))')
    shards = sorted(set(root.glob('parallel-*/publisher.sqlite')) | set(root.glob('shards/*/parallel-*/publisher.sqlite')))
    merged = 0
    used = 0
    with sqlite3.connect(main, timeout=30) as main_conn:
        main_conn.execute('PRAGMA busy_timeout=30000')
        for shard in shards:
            for attempt in range(1, 6):
                try:
                    with sqlite3.connect('file:' + str(shard) + '?mode=ro', uri=True, timeout=30) as source:
                        source.execute('PRAGMA busy_timeout=30000')
                        rows = source.execute('SELECT vendor,key,hash FROM published').fetchall()
                    before = main_conn.total_changes
                    main_conn.executemany('INSERT OR REPLACE INTO published(vendor,key,hash) VALUES (?,?,?)', rows)
                    main_conn.commit()
                    merged += main_conn.total_changes - before
                    used += 1
                    break
                except sqlite3.OperationalError as error:
                    if 'locked' in str(error).lower() and attempt < 5:
                        print(json.dumps({'event': 'shard_merge_retry', 'path': str(shard), 'attempt': attempt}), flush=True)
                        time.sleep(2 * attempt)
                        continue
                    print(json.dumps({'event': 'shard_merge_skipped', 'path': str(shard), 'error': str(error)[:120]}), flush=True)
                    break
                except sqlite3.Error as error:
                    print(json.dumps({'event': 'shard_merge_skipped', 'path': str(shard), 'error': type(error).__name__}), flush=True)
                    break
    with sqlite3.connect(main, timeout=30) as conn:
        total = conn.execute('SELECT count(*) FROM published').fetchone()[0]
    encrypt_file('publisher', main)
    return {'status': 'merged', 'shards_seen': len(shards), 'shards_merged': used, 'rows_merged': merged, 'published_rows': total}


def main():
    result = merge(ROOT)
    print(json.dumps({'event': 'shard_checkpoint_merged', **result}, ensure_ascii=False), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
