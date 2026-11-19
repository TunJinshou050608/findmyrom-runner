"""Exercise the official D1 schema migrations and stock writers across baseline -> v5 -> v6 -> v7.
No firmware payloads and no external network calls.
"""
import json
from pathlib import Path
import pytest

from fmr_core.crypto import (
    build_boot_payload,
    build_firmware_payload,
    generate_keypair,
    sign_boot_image,
    verify_signature,
)
from fmr_core.d1_client import D1Client
from fmr_core.schema_migrations import CURRENT, ensure_schema
from fmr_ingest_stock.device_sync import ensure_base_brands_and_anchor, sync_devices
from fmr_ingest_stock.firmware_sync import sync_firmwares

ROOT = Path(__file__).parents[1]


@pytest.fixture
def db(tmp_path, monkeypatch):
    private, public = generate_keypair()
    monkeypatch.setenv('SIGN_PRIVATE_KEY_HEX', private)
    x = D1Client(local_sqlite_path=str(tmp_path / 'contract.sqlite'))
    x.execute_script((ROOT / 'schema.sql').read_text())
    ensure_base_brands_and_anchor(x)
    yield x, private, public
    x.close()


def _seed_firmware(x, tmp_path, codename='alioth', source_codename='alioth_global', version='V14.0.1.0.TKHMIXM', filename='fw.json'):
    dev_file = tmp_path / f'dev_{filename}'
    fw_file = tmp_path / filename
    dev_file.write_text(json.dumps([{'codename': codename, 'brand': 'Xiaomi', 'name': 'POCO F3', 'android_release': 13}]))
    fw_file.write_text(
        json.dumps(
            [
                {
                    'codename': source_codename,
                    'version': version,
                    'link': f'https://bigota.d.miui.com/{version}/{source_codename}_{version}.zip',
                    'android': '13.0',
                    'md5': 'a' * 32,
                    'date': '2024-01-15',
                }
            ]
        )
    )
    sync_devices(x, str(dev_file))
    sync_firmwares(x, str(fw_file), priority_codenames={codename})
    return x.query(
        'SELECT * FROM stock_firmwares WHERE device_codename=? AND version_str=? ORDER BY id DESC LIMIT 1',
        (codename, version),
    )[0]


def test_migration_preserves_download_identity_extraction_state_and_foreign_keys(db, tmp_path):
    x, private, public = db
    fw = _seed_firmware(x, tmp_path)
    x.execute("UPDATE stock_firmwares SET extract_status='processing',extract_retries=2 WHERE id=?", (fw['id'],))
    x.execute(
        """INSERT INTO extracted_boot_images(public_token,firmware_id,device_codename,version_str,partition_name,variant_type,file_name,file_size_bytes,sha256_hash,gh_private_repo,gh_release_tag,gh_asset_id,ed25519_signature)
        VALUES ('bt_contract001',?,'alioth','V14.0.1.0.TKHMIXM','boot','stock','boot.img.gz',123,?,'owner/vault','release',1,?)""",
        (fw['id'], 'a' * 64, sign_boot_image('bt_contract001', 'a' * 64, 'owner/vault', 1, private)),
    )
    x.execute("UPDATE sqlite_sequence SET seq=1000 WHERE name='stock_firmwares'")

    ensure_schema(x, allow_migration=True, through=6)
    assert not x.query('PRAGMA foreign_key_check')
    after = x.query('SELECT * FROM stock_firmwares WHERE id=?', (fw['id'],))[0]
    for column in ('id', 'public_token', 'original_official_url', 'fast_cdn_url', 'ed25519_signature'):
        assert after[column] == fw[column]
    assert after['extract_status'] == 'processing' and after['extract_retries'] == 2
    assert x.query('SELECT firmware_id FROM extracted_boot_images')[0]['firmware_id'] == fw['id']

    image = x.query('SELECT * FROM extracted_boot_images')[0]
    assert verify_signature(
        build_boot_payload(image['public_token'], image['sha256_hash'], image['gh_private_repo'], image['gh_asset_id']),
        image['ed25519_signature'],
        public,
    )
    assert verify_signature(
        build_firmware_payload(after['public_token'], after['fast_cdn_url']),
        after['ed25519_signature'],
        public,
    )

    # A model-aware v6 schema keeps a second model_code under the same codename separate.
    other = _seed_firmware(x, tmp_path, codename='alioth', source_codename='alioth_eea_global', filename='fw2.json')
    assert other['public_token'] != fw['public_token']
    assert x.query("SELECT seq FROM sqlite_sequence WHERE name='stock_firmwares'")[0]['seq'] >= 1000
    assert len(x.query('SELECT * FROM stock_firmwares')) == 2
    assert x.query('SELECT official FROM catalogue_coverage')[0]['official'] == 2


def test_yaml_official_writer_accepts_trigger_inflated_changes(db, tmp_path):
    x, _, _ = db
    ensure_schema(x, allow_migration=True, through=6)
    native = x.execute_batch

    def with_trigger_counts(statements):
        before = x._get_sqlite_conn().total_changes
        result = native(statements)
        result['changes'] = x._get_sqlite_conn().total_changes - before
        return result

    x.execute_batch = with_trigger_counts
    source = tmp_path / 'firmwares.json'
    source.write_text(
        json.dumps([{'codename': 'alioth', 'version': 'V14.1', 'link': 'https://bigota.d.miui.com/V14.1/alioth.zip', 'android': '14'}])
    )
    assert sync_firmwares(x, str(source))['inserted_or_updated'] == 1
    row = x.query("SELECT public_token FROM stock_firmwares WHERE device_codename='alioth'")[0]
    assert sync_firmwares(x, str(source))['inserted_or_updated'] == 1
    assert x.query("SELECT public_token FROM stock_firmwares WHERE device_codename='alioth'") == [row]


def test_version_checks_are_read_only_until_migration_and_receipts_are_reusable(db):
    x, _, _ = db
    before = x._get_sqlite_conn().total_changes
    with pytest.raises(RuntimeError, match='migration required'):
        ensure_schema(x)
    assert x._get_sqlite_conn().total_changes == before
    ensure_schema(x, allow_migration=True)
    saved = x.query('SELECT * FROM fmr_schema_migrations')
    x._fmr_schema_ready = 0
    ensure_schema(x, allow_migration=True)
    assert x.query('SELECT * FROM fmr_schema_migrations') == saved
    x.execute("UPDATE fmr_schema_migrations SET sha256='bad' WHERE version=5")
    x._fmr_schema_ready = 0
    with pytest.raises(RuntimeError, match='checksum mismatch'):
        ensure_schema(x)


def test_model_upgrade_refuses_unknown_columns_instead_of_dropping_data(db):
    x, _, _ = db
    ensure_schema(x, allow_migration=True)
    x.execute('ALTER TABLE stock_firmwares ADD COLUMN unexpected TEXT')
    with pytest.raises(RuntimeError, match='Unexpected firmware columns'):
        ensure_schema(x, allow_migration=True, through=6)
    assert 'unexpected' in {r['name'] for r in x.query('PRAGMA table_info(stock_firmwares)')}
    assert not x.query('SELECT * FROM fmr_schema_migrations WHERE version=6')


def test_compact_search_preserves_mid_version_matching_and_tracks_rekeys_deletes(db, tmp_path):
    x, _, _ = db
    first = _seed_firmware(x, tmp_path, version='BUILD.123', filename='s1.json')
    ensure_schema(x, allow_migration=True, through=6)
    query = lambda term: x.query('SELECT device_codename FROM firmware_search WHERE versions LIKE ?', ('%' + term + '%',))
    assert query('ILD.12') == [{'device_codename': 'alioth'}]
    _seed_firmware(x, tmp_path, version='SECOND.456', filename='s2.json')
    assert len(x.query('SELECT * FROM firmware_search')) == 1
    x.execute("UPDATE stock_firmwares SET version_str='OTHER.123' WHERE public_token=?", (first['public_token'],))
    assert not query('BUILD') and query('HER.12')
    x.execute('DELETE FROM stock_firmwares WHERE public_token=?', (first['public_token'],))
    assert query('SECOND')
    x.execute('DELETE FROM stock_firmwares')
    assert not x.query('SELECT * FROM firmware_search')


def test_schema_v7_delivery_specs_upgrade_and_current_constant(db):
    x, _, _ = db
    assert CURRENT == 8
    ensure_schema(x, allow_migration=True, through=8)
    assert [r['version'] for r in x.query('SELECT version FROM fmr_schema_migrations ORDER BY version')] == [4, 5, 6, 7, 8]
    x.execute(
        "INSERT INTO firmware_delivery_specs(public_token,delivery_mode,resolver_json,content_hash) VALUES ('fw_v7','direct_cdn','{}','hash0')"
    )
    assert x.query("SELECT delivery_mode FROM firmware_delivery_specs WHERE public_token='fw_v7'") == [
        {'delivery_mode': 'direct_cdn'}
    ]
