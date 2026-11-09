"""Additive metadata contract, independent of the historical v6 table rebuild."""
import argparse

DDL = '''CREATE TABLE IF NOT EXISTS firmware_metadata (
 public_token TEXT PRIMARY KEY,
 metadata_json TEXT NOT NULL CHECK(json_valid(metadata_json)),
 content_hash TEXT NOT NULL,
 updated_at INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);'''


def ensure(db, apply=False):
    if getattr(db, '_firmware_metadata_ready', False):
        return
    if apply:
        db.execute_script(DDL)
    columns = db.query('PRAGMA table_info(firmware_metadata)')
    if [(c['name'],c['type'].upper()) for c in columns] != [('public_token','TEXT'),('metadata_json','TEXT'),('content_hash','TEXT'),('updated_at','INTEGER')]:
        raise RuntimeError('Firmware metadata schema not ready')
    fk = db.query('PRAGMA foreign_key_list(firmware_metadata)')
    if fk:
        raise RuntimeError('Unexpected firmware metadata relationship')
    db._firmware_metadata_ready = True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    from .d1_client import D1Client
    db = D1Client()
    try:
        ensure(db, apply=args.apply)
    finally:
        db.close()


if __name__ == '__main__':
    main()
