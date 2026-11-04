"""Turn classified reprocess records into guarded D1 UPDATE statements.

Only ``decision=apply`` records are emitted, and every statement is guarded by
the old values captured during the reprocess export. A concurrent edit or a
stale snapshot therefore updates zero rows instead of overwriting newer data.
"""
import argparse
import json
from pathlib import Path

FIELDS = ('project_name', 'build_version_tag', 'android_version')
OLD_KEYS = {'project_name': 'project_name', 'build_version_tag': 'project_version', 'android_version': 'android_version'}


def sql_literal(value):
    if value is None:
        return 'NULL'
    text = str(value)
    if '\x00' in text:
        raise ValueError('NUL byte in value')
    return "'" + text.replace("'", "''") + "'"


def guard(field, value):
    return field + (' IS NULL' if value is None else ' IS ' + sql_literal(value))


def statement(record):
    proposed = record.get('proposed') or {}
    fields = [field for field in FIELDS if field in proposed]
    if not fields:
        raise ValueError('no supported fields')
    old = record.get('old') or {}
    assignments = ','.join(field + '=' + sql_literal(proposed[field]) for field in fields)
    conditions = ['id=' + str(int(record['id']))]
    conditions.extend(guard(field, old.get(OLD_KEYS[field])) for field in FIELDS)
    return 'UPDATE custom_resources SET ' + assignments + ' WHERE ' + ' AND '.join(conditions) + ';'


def build(records, chunk_size=100):
    statements = [statement(record) for record in records if record.get('decision') == 'apply']
    chunks = [statements[offset:offset + chunk_size] for offset in range(0, len(statements), chunk_size)]
    return statements, chunks


def write_chunks(chunks, out_dir):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths = []
    for index, chunk in enumerate(chunks, 1):
        path = out / f'corrections_{index:03d}.sql'
        path.write_text('\n'.join(chunk) + '\n')
        paths.append(str(path))
    return paths


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True)
    parser.add_argument('--out-dir', required=True)
    parser.add_argument('--chunk-size', type=int, default=100)
    args = parser.parse_args(argv)

    records = [json.loads(line) for line in Path(args.input).read_text().splitlines() if line.strip()]
    statements, chunks = build(records, args.chunk_size)
    paths = write_chunks(chunks, args.out_dir)
    print(json.dumps({
        'apply_rows': len(statements),
        'chunks': len(chunks),
        'chunk_size': args.chunk_size,
        'files': paths,
    }, ensure_ascii=False))


if __name__ == '__main__':
    main()
