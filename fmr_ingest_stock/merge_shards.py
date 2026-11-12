"""Merge durable shard receipts without replacing newer confirmations."""
import hashlib
import json
from pathlib import Path
import sqlite3

from .catalogue_publish import open_ledger
from .staged_catalogue import ROOT, encrypt_file


def merge_one(conn, shard):
    stat = shard.stat()
    marker = 'merged_shard:' + hashlib.sha256(str(shard.resolve()).encode()).hexdigest()
    fingerprint = f'{stat.st_mtime_ns}:{stat.st_size}'
    old = conn.execute('SELECT value FROM control WHERE key=?',(marker,)).fetchone()
    if old and old[0] == fingerprint:
        return None
    conn.execute('ATTACH DATABASE ? AS shard',(shard.resolve().as_uri()+'?mode=ro',))
    try:
        conn.execute('BEGIN IMMEDIATE')
        before = conn.total_changes
        tables = {r[0] for r in conn.execute("SELECT name FROM shard.sqlite_master WHERE type='table'")}
        # Legacy shards contain full baseline copies. They can fill missing
        # receipts, but cannot establish that an existing receipt is older.
        conn.execute('INSERT OR IGNORE INTO published SELECT vendor,key,hash FROM shard.published')
        if 'publication_ack_stamp' in tables:
            conn.execute("""INSERT INTO published(vendor,key,hash)
                SELECT s.vendor,s.key,s.hash FROM shard.publication_ack_stamp s
                JOIN shard.published p ON p.vendor=s.vendor AND p.key=s.key AND p.hash=s.hash
                LEFT JOIN main.publication_ack_stamp m ON m.vendor=s.vendor AND m.key=s.key
                WHERE m.confirmed_at IS NULL OR s.confirmed_at>m.confirmed_at
                ON CONFLICT(vendor,key) DO UPDATE SET hash=excluded.hash
                WHERE published.hash!=excluded.hash""")
            conn.execute("""INSERT INTO publication_ack_stamp(vendor,key,hash,confirmed_at)
                SELECT s.vendor,s.key,s.hash,s.confirmed_at FROM shard.publication_ack_stamp s
                JOIN shard.published p ON p.vendor=s.vendor AND p.key=s.key AND p.hash=s.hash
                WHERE true ON CONFLICT(vendor,key) DO UPDATE
                SET hash=excluded.hash,confirmed_at=excluded.confirmed_at
                WHERE excluded.confirmed_at>publication_ack_stamp.confirmed_at""")
        if 'xiaomi_publication_review' in tables:
            conn.execute("""INSERT INTO xiaomi_publication_review(key,hash,retry_at)
                SELECT key,hash,retry_at FROM shard.xiaomi_publication_review WHERE true
                ON CONFLICT(key) DO UPDATE SET hash=excluded.hash,retry_at=excluded.retry_at
                WHERE excluded.retry_at>xiaomi_publication_review.retry_at""")
        changed = conn.total_changes - before
        conn.execute('INSERT OR REPLACE INTO control(key,value) VALUES (?,?)',(marker,fingerprint))
        conn.commit()
        return changed
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute('DETACH DATABASE shard')


def merge(root):
    root = Path(root)
    main = root/'publisher.sqlite'
    shards = sorted(set(root.glob('parallel-*/publisher.sqlite')) | set(root.glob('shards/*/parallel-*/publisher.sqlite')))
    ledger = open_ledger(main)
    merged = used = unchanged = 0
    try:
        conn = ledger._get_sqlite_conn()
        conn.execute('PRAGMA busy_timeout=30000')
        for shard in shards:
            result = merge_one(conn,shard)
            if result is None:
                unchanged += 1
            else:
                used += 1
                merged += result
        total = conn.execute('SELECT count(*) FROM published').fetchone()[0]
    finally:
        ledger.close()
    encrypt_file('publisher',main)
    return {'status':'merged','shards_seen':len(shards),'shards_merged':used,
            'shards_unchanged':unchanged,'local_changes':merged,'published_rows':total}


def main():
    print(json.dumps({'event':'shard_checkpoint_merged',**merge(ROOT)},ensure_ascii=False),flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
