"""Exercise the actual writers against baseline -> v5 -> model-aware v6 D1.
No firmware payloads and no external network calls.
"""
import json
from pathlib import Path
import sqlite3
import pytest
from fmr_core.d1_client import D1Client
from fmr_core.schema_migrations import ensure_schema
from fmr_core.crypto import generate_keypair,verify_signature,build_firmware_payload,sign_boot_image,build_boot_payload
from fmr_ingest_stock.catalogue_sync import publish_release
from fmr_ingest_stock.device_sync import ensure_base_brands_and_anchor

ROOT=Path(__file__).parents[1]

@pytest.fixture
def db(tmp_path,monkeypatch):
 private,public=generate_keypair();monkeypatch.setenv('SIGN_PRIVATE_KEY_HEX',private)
 x=D1Client(local_sqlite_path=str(tmp_path/'contract.sqlite'))
 x.execute_script((ROOT/'schema.sql').read_text());ensure_base_brands_and_anchor(x)
 yield x,private,public
 x.close()

def item(model='sample'):
 return {'vendor':'oneplus','upstream_id':model,'codename':model,'canonical_code':'oneplus-shared','name':'Sample'}

def release():return {'version':'BUILD.123','region':'EU','kind':'full','download_url':'https://ota.oneplus.net/sample.zip'}


def test_migration_preserves_download_identity_extraction_state_and_foreign_keys(db):
 x,private,public=db
 first=publish_release(x,item(),release());fw=x.query('SELECT * FROM stock_firmwares')[0]
 x.execute("UPDATE stock_firmwares SET extract_status='processing',extract_retries=2 WHERE id=?",(fw['id'],))
 x.execute("""INSERT INTO extracted_boot_images(public_token,firmware_id,device_codename,version_str,partition_name,variant_type,file_name,file_size_bytes,sha256_hash,gh_private_repo,gh_release_tag,gh_asset_id,ed25519_signature)
 VALUES ('bt_contract001',?,'oneplus-shared','BUILD.123','boot','stock','boot.img.gz',123,?,'owner/vault','release',1,?)""",(fw['id'],'a'*64,sign_boot_image('bt_contract001','a'*64,'owner/vault',1,private)))
 x.execute("UPDATE sqlite_sequence SET seq=1000 WHERE name='stock_firmwares'")
 ensure_schema(x,allow_migration=True,through=6)
 assert not x.query('PRAGMA foreign_key_check')
 after=x.query('SELECT * FROM stock_firmwares')[0]
 for column in ('id','public_token','original_official_url','fast_cdn_url','ed25519_signature'):assert after[column]==fw[column]
 assert after['extract_status']=='processing' and after['extract_retries']==2
 assert x.query('SELECT firmware_id FROM extracted_boot_images')[0]['firmware_id']==fw['id']
 image=x.query('SELECT * FROM extracted_boot_images')[0]
 assert verify_signature(build_boot_payload(image['public_token'],image['sha256_hash'],image['gh_private_repo'],image['gh_asset_id']),image['ed25519_signature'],public)
 assert verify_signature(build_firmware_payload(after['public_token'],after['fast_cdn_url']),after['ed25519_signature'],public)
 assert publish_release(x,item(),release())['public_token']==first['public_token']
 # A model-aware schema keeps a second model separate without rekeying the first.
 other=publish_release(x,item('another-model'),release())
 assert other['public_token']!=first['public_token']
 assert x.query("SELECT seq FROM sqlite_sequence WHERE name='stock_firmwares'")[0]['seq']>=1000
 assert len(x.query('SELECT * FROM stock_firmwares'))==2
 assert x.query('SELECT official FROM catalogue_coverage')[0]['official']==2


def test_legacy_model_collision_is_pending_until_model_identity_is_supported(db,tmp_path):
 x,_,_=db
 from fmr_ingest_stock.catalogue_publish import sync_one,open_ledger
 first=publish_release(x,item(),release());ledger=open_ledger(tmp_path/'ledger.sqlite')
 try:
  before=x.query('SELECT * FROM stock_firmwares')
  assert not sync_one(x,ledger,'oneplus',('release','new-model',{'device':item('another-model'),'release':release()},'hash'))
  assert not ledger.query('SELECT key FROM published')
  assert x.query('SELECT * FROM stock_firmwares')==before
  ensure_schema(x,allow_migration=True,through=6)
  assert sync_one(x,ledger,'oneplus',('release','new-model',{'device':item('another-model'),'release':release()},'hash'))
  assert ledger.query('SELECT key FROM published')==[{'key':'new-model'}]
  rows=x.query('SELECT public_token,model_code FROM stock_firmwares ORDER BY id')
  assert len(rows)==2 and rows[0]['public_token']==first['public_token'] and rows[1]['model_code']=='another-model'
 finally:ledger.close()


def test_yaml_official_writer_accepts_trigger_inflated_changes(db,tmp_path):
 x,_,_=db
 from fmr_ingest_stock.firmware_sync import sync_firmwares
 ensure_schema(x,allow_migration=True,through=6)
 native=x.execute_batch
 def with_trigger_counts(statements):
  before=x._get_sqlite_conn().total_changes;result=native(statements)
  result['changes']=x._get_sqlite_conn().total_changes-before
  return result
 x.execute_batch=with_trigger_counts
 source=tmp_path/'firmwares.json';source.write_text(json.dumps([{'codename':'alioth','version':'V14.1','link':'https://bigota.d.miui.com/V14.1/alioth.zip','android':'14'}]))
 assert sync_firmwares(x,str(source))['inserted_or_updated']==1
 row=x.query("SELECT public_token FROM stock_firmwares WHERE device_codename='alioth'")[0]
 assert sync_firmwares(x,str(source))['inserted_or_updated']==1
 assert x.query("SELECT public_token FROM stock_firmwares WHERE device_codename='alioth'")==[row]


def test_version_checks_are_read_only_until_migration_and_receipts_are_reusable(db):
 x,_,_=db
 before=x._get_sqlite_conn().total_changes
 with pytest.raises(RuntimeError,match='migration required'):ensure_schema(x)
 assert x._get_sqlite_conn().total_changes==before
 ensure_schema(x,allow_migration=True)
 saved=x.query('SELECT * FROM fmr_schema_migrations')
 x._fmr_schema_ready=0;ensure_schema(x,allow_migration=True)
 assert x.query('SELECT * FROM fmr_schema_migrations')==saved
 x.execute("UPDATE fmr_schema_migrations SET sha256='bad' WHERE version=5");x._fmr_schema_ready=0
 with pytest.raises(RuntimeError,match='checksum mismatch'):ensure_schema(x)


def test_model_upgrade_refuses_unknown_columns_instead_of_dropping_data(db):
 x,_,_=db;ensure_schema(x,allow_migration=True)
 x.execute('ALTER TABLE stock_firmwares ADD COLUMN unexpected TEXT')
 with pytest.raises(RuntimeError,match='Unexpected firmware columns'):ensure_schema(x,allow_migration=True,through=6)
 assert 'unexpected' in {r['name'] for r in x.query('PRAGMA table_info(stock_firmwares)')}
 assert not x.query('SELECT * FROM fmr_schema_migrations WHERE version=6')


def test_workflow_orders_schema_before_all_d1_publishers():
 import yaml
 workflow=yaml.safe_load((ROOT/'.github/workflows/catalogue_publish.yml').read_text())
 assert workflow['jobs']['schema']['concurrency']['group']=='d1-schema-maintenance'
 for name in ('publish','samsung','telegram'):assert workflow['jobs'][name]['needs']=='schema'
 assert 'CF_API_TOKEN' not in (ROOT/'.github/workflows/telegram_fetch.yml').read_text()
 assert 'CF_API_TOKEN' not in (ROOT/'.github/workflows/telegram_clean.yml').read_text()
 # The collection/cleaning contracts remain local and independent of D1 schemas.
 assert 'local_runtime clean' in (ROOT/'.github/workflows/telegram_clean.yml').read_text()
 assert workflow['jobs']['telegram']['steps'][-1]['run'].startswith('python -u -m fmr_tg_scanner.local_runtime publish')


def test_compact_search_preserves_mid_version_matching_and_tracks_rekeys_deletes(db):
 x,_,_=db
 first=publish_release(x,item(),release());ensure_schema(x,allow_migration=True,through=6)
 query=lambda term:x.query('SELECT device_codename FROM firmware_search WHERE versions LIKE ?',('%'+term+'%',))
 assert query('ILD.12')==[{'device_codename':'oneplus-shared'}]
 publish_release(x,item(),{**release(),'download_url':'https://ota.oneplus.net/sample.tgz'})
 assert len(x.query('SELECT * FROM firmware_search'))==1
 x.execute("UPDATE stock_firmwares SET version_str='OTHER.123'")
 assert not query('BUILD') and query('HER.12')
 x.execute('DELETE FROM stock_firmwares WHERE public_token=?',(first['public_token'],))
 assert query('OTHER')
 x.execute('DELETE FROM stock_firmwares')
 assert not x.query('SELECT * FROM firmware_search')
