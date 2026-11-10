"""Deterministic public Markdown projections; never export private download data."""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from urllib.parse import quote, urlsplit

from fmr_ingest_stock.release_metadata import package_label
from fmr_ingest_stock.d1_budget import BudgetDB as D1Client

MAX_BYTES = 512_000
IMAGE_HEADER = (
    '| Firmware Version | Partition | Variant | Size | SHA-256 (Prefix) | Download |\n'
    '| :--- | :--- | :--- | :--- | :--- | :--- |\n'
)


def public_site(value: str) -> str:
    value = value.rstrip('/')
    u = urlsplit(value)
    if u.scheme != 'https' or not u.hostname or u.username or u.password or u.query or u.fragment or u.path:
        raise ValueError('Public site must be an HTTPS origin')
    return value


def cell(value) -> str:
    text = html.escape(str(value or ''), quote=True).replace('\r', '').replace('\n', '')
    for char in ('\\', '|', '[', ']', '`', '*', '_'):
        text = text.replace(char, '\\' + char)
    return text


def token_url(site, token, prefix):
    if not re.fullmatch(prefix + r'[A-Za-z0-9_-]{10,12}', token):
        raise ValueError('Invalid public token')
    return site + '/verify/' + token


def generate_jumper_markdown_tree(db_client, output_dir: Path, base_site_url: str = 'https://findmyrom.com') -> dict:
    site = public_site(base_site_url)
    devices = db_client.query("""SELECT codename,brand_id,marketing_name FROM devices
        WHERE codename!='__global__' ORDER BY brand_id ASC,codename ASC""")
    # One ranked union chooses the newest 50 *versions*, shared by both tables.
    versions = """WITH versions AS (
        SELECT device_codename,version_str,MAX(COALESCE(published_date,'')) AS release_date FROM stock_firmwares GROUP BY device_codename,version_str
        UNION ALL SELECT i.device_codename,i.version_str,MAX(COALESCE(f.published_date,'')) FROM extracted_boot_images i LEFT JOIN stock_firmwares f ON f.id=i.firmware_id GROUP BY i.device_codename,i.version_str),
        combined AS (SELECT device_codename,version_str,MAX(release_date) AS release_date FROM versions GROUP BY device_codename,version_str),
        ranked AS (SELECT device_codename,version_str,ROW_NUMBER() OVER
          (PARTITION BY device_codename ORDER BY release_date DESC,version_str DESC) AS rn FROM combined)
    """
    images = db_client.query(versions + """SELECT i.device_codename,i.version_str,i.partition_name,
        i.variant_type,i.file_size_bytes,i.sha256_hash,i.public_token FROM extracted_boot_images i
        JOIN ranked r USING(device_codename,version_str) WHERE r.rn<=50 AND i.device_codename!='__global__'
        ORDER BY i.device_codename,i.version_str DESC,i.partition_name,i.variant_type,i.public_token""")
    firmwares = db_client.query(versions + """SELECT f.device_codename,f.version_str,f.region_code,
        f.model_code,f.package_type,f.public_token FROM stock_firmwares f
        JOIN ranked r USING(device_codename,version_str) WHERE r.rn<=50 AND f.device_codename!='__global__'
        ORDER BY f.device_codename,f.version_str DESC,f.region_code,f.package_type,f.public_token""")
    # Public projection consumes the same stored display metadata as the website.
    labels={}
    for offset in range(0,len(firmwares),60):
        chunk=firmwares[offset:offset+60];tokens=[r['public_token'] for r in chunk]
        if not tokens:continue
        try:
            metadata=db_client.query('SELECT public_token,metadata_json FROM firmware_metadata WHERE public_token IN ('+','.join('?' for _ in tokens)+')',tokens)
        except Exception as exc:
            if 'no such table' in str(exc).lower() and 'firmware_metadata' in str(exc):break
            raise
        by_token={r['public_token']:json.loads(r['metadata_json']) for r in metadata}
        for row in chunk:
            m=by_token.get(row['public_token'],{})
            if m.get('schema')==1 and isinstance(m.get('display_version'),str):
                row['display_version']=m['display_version']
                if isinstance(m.get('region'),str):row['display_region']=m['region']
                labels[(row['device_codename'],row['version_str'])]=m['display_version']
    grouped_images, grouped_fw = {}, {}
    for row in images:
        grouped_images.setdefault(row['device_codename'], []).append(row)
    for row in firmwares:
        grouped_fw.setdefault(row['device_codename'], []).append(row)
    files = {}
    index_rows = []
    skipped = 0
    for d in devices:
        code = d['codename']
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', code):
            raise ValueError('Unsafe device codename')
        ims, fws = grouped_images.get(code, []), grouped_fw.get(code, [])
        if not ims and not fws:
            skipped += 1
            continue
        device_url = site + '/devices/' + quote(code, safe='')
        title = cell(d['marketing_name']) + ' (' + cell(code) + ')'
        header = '# ' + title + '\n\n[All versions and regions](' + device_url + ')\n\n' + IMAGE_HEADER
        rows = []
        for i in ims:
            partition = str(i['partition_name']).removesuffix('.img') + '.img'
            sha = str(i['sha256_hash'])
            if not re.fullmatch(r'[a-f0-9]{64}', sha):
                raise ValueError('Invalid image digest')
            rows.append('| ' + ' | '.join([
                cell(labels.get((i['device_codename'],i['version_str']),i['version_str'])), cell(partition), cell(i['variant_type']),
                f"{i['file_size_bytes'] / 1048576:.1f} MB", '`' + sha[:12] + '...`',
                '[Download](' + token_url(site, i['public_token'], 'bt_') + ')',
            ]) + ' |\n')
        footer = '\n## Official firmware\n\n| Firmware Version | Region / Variant | Package | Download |\n| :--- | :--- | :--- | :--- |\n'
        fw_rows = ['| ' + ' | '.join([
            cell(f.get('display_version') or f['version_str']), cell(f.get('display_region') or f['region_code']) + ' / ' + cell(f['model_code'] or code),
            cell(package_label(d['brand_id'],f['package_type'])), '[Download](' + token_url(site, f['public_token'], 'fw_') + ')',
        ]) + ' |\n' for f in fws]
        # Bound a pathological device without emitting partial Markdown rows.
        ending = '\n[More versions on FindMyROM](' + device_url + ')\n'
        sections = header
        for row in rows:
            if len((sections + row + footer + ending).encode()) <= MAX_BYTES:
                sections += row
        sections += footer
        for row in fw_rows:
            if len((sections + row + ending).encode()) <= MAX_BYTES:
                sections += row
        sections += ending
        files['devices/' + code + '.md'] = sections.encode('utf-8')
        index_rows.append('| ' + cell(d['brand_id']) + ' | [' + title + '](devices/' + quote(code, safe='') + '.md) |\n')
    files['README.md'] = ('# Android firmware & boot images\n\n'
        'Official firmware and standalone boot image index. Downloads open the verification page on '
        '[FindMyROM](' + site + '/).\n\n| Brand | Device |\n| :--- | :--- |\n' + ''.join(index_rows)).encode('utf-8')
    if any(len(content) > MAX_BYTES for content in files.values()):
        raise ValueError('Markdown size limit exceeded')
    digest = hashlib.sha256()
    for name in sorted(files):
        digest.update(files[name])
    output_dir = Path(output_dir)
    if output_dir.is_symlink():
        raise ValueError('Output directory cannot be a symlink')
    output_dir.mkdir(parents=True, exist_ok=True)
    if any(p.is_symlink() for p in output_dir.rglob('*')):
        raise ValueError('Output tree cannot contain symlinks')
    # Complete all reads/rendering before modifying the previous good projection.
    with tempfile.TemporaryDirectory(prefix='.jumper-', dir=output_dir.parent) as stage:
        for name, content in files.items():
            path = Path(stage) / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        for name in sorted(files):
            path = output_dir / name
            path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(Path(stage) / name, path)
        for old in output_dir.rglob('*.md'):
            if old.relative_to(output_dir).as_posix() not in files:
                old.unlink()
    return {'devices_rendered': len(index_rows), 'skipped_empty_devices': skipped,
            'total_files_written': len(files), 'tree_sha256': digest.hexdigest()}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', required=True, type=Path)
    parser.add_argument('--base-site-url', default=os.environ.get('PUBLIC_SITE_URL', 'https://findmyrom.com'))
    args = parser.parse_args(argv)
    db = None
    try:
        # A local SQLite projection is safe during the production quota pause.
        if not os.environ.get('LOCAL_SQLITE_PATH'):
            from fmr_ingest_stock.d1_budget import NOT_BEFORE
            import time
            if time.time() < NOT_BEFORE:
                raise RuntimeError('Production reads paused until UTC November 1')
        db = D1Client()
        if db.mode!='sqlite' and not db.acquire():
            print('Shared D1 allocation unavailable; public projection unchanged.');return 1
        print(json.dumps(generate_jumper_markdown_tree(db, args.output_dir, args.base_site_url), sort_keys=True))
        return 0
    except Exception as exc:
        print(f'Jumper export failed ({type(exc).__name__})', file=sys.stderr)
        return 1
    finally:
        if db:
            try:db.settle()
            finally:db.close()


if __name__ == '__main__':
    sys.exit(main())
