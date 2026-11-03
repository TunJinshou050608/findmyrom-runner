"""Deterministic SQL transaction fixture for native D1 replay in Web CI."""
from contextlib import redirect_stdout
from datetime import datetime,timezone
import io
import json
from pathlib import Path
import tempfile
from fmr_core.d1_client import D1Client,split_sql_statements
from fmr_core.schema_migrations import ensure_schema
from fmr_core.identity_maintenance import migrate

ROOT=Path(__file__).parents[1]


def export():
    batches=[]
    class Capture(D1Client):
        def execute_batch(self,items):
            result=super().execute_batch(items)
            batches.append([{'sql':s,'params':list(p)} for s,p in items]);return result
        def execute_script(self,script,**kwargs):
            super().execute_script(script,**kwargs)
            batches.append([{'sql':s,'params':[]} for s in split_sql_statements(script)])
        def execute(self,sql,params=(),**kwargs):
            result=super().execute(sql,params,**kwargs)
            # The day marker's started_at is deterministic for fixture stability.
            batches.append([{'sql':sql,'params':list(params)}]);return result
    with tempfile.TemporaryDirectory() as tmp:
        db=Capture(local_sqlite_path=tmp+'/native.sqlite')
        try:
            db.execute_script((ROOT/'schema.sql').read_text())
            db.execute("INSERT INTO brands(id,name,bl_unlock_status) VALUES ('xiaomi','Xiaomi','unknown')")
            db.execute("INSERT INTO devices(codename,brand_id,marketing_name,phase2_roms_enabled) VALUES ('sample','xiaomi','Sample',1)")
            for i in range(1,3):
                db.execute("INSERT INTO stock_firmwares(public_token,device_codename,model_code,region_code,version_str,android_version,package_type,original_official_url,fast_cdn_url,ed25519_signature) VALUES (?,'sample','model-a','Global',?,'14','zip','url','url','sig')",('fw_native00'+str(i),'V'+str(i)))
            db.execute("INSERT INTO extracted_boot_images(public_token,firmware_id,device_codename,version_str,partition_name,variant_type,file_name,file_size_bytes,sha256_hash,gh_private_repo,gh_release_tag,gh_asset_id,ed25519_signature) VALUES ('bt_native001',1,'sample','V1','boot','stock','boot.img.gz',123,'hash','owner/vault','release',1,'sig')")
            db.execute("UPDATE sqlite_sequence SET seq=1000 WHERE name IN ('stock_firmwares','extracted_boot_images')")
            ensure_schema(db,allow_migration=True,through=5)
            with redirect_stdout(io.StringIO()):
                assert migrate(db,now=lambda:datetime(2026,11,4,1,tzinfo=timezone.utc),batch_size=1)=='complete'
        finally:db.close()
    return batches


if __name__=='__main__':
    print(json.dumps(export(),indent=2,ensure_ascii=False))
