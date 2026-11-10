import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from fmr_core.d1_client import D1Client
from fmr_extractor.jumper_sync import generate_jumper_markdown_tree

ROOT = Path(__file__).resolve().parents[1]
PRIVATE = ['PRIVATE_ASSET_999', 'secret_owner/private_vault', 'SECRET_SIG', 'https://secret.invalid/fw.zip']


def fixture(tmp_path):
    db = D1Client(local_sqlite_path=str(tmp_path / 'sample.db'))
    db.execute_script((ROOT / 'schema.sql').read_text())
    db.execute("INSERT INTO brands(id,name,bl_unlock_status) VALUES ('xiaomi','Xiaomi','easy')")
    for code in ['__global__', 'empty', 'marble', 'alioth']:
        db.execute("INSERT INTO devices(codename,brand_id,marketing_name) VALUES (?,'xiaomi',?)", (code, 'Phone ' + code))
    for code, token in [('marble', 'fw_aaaaaaaaaa'), ('alioth', 'fw_bbbbbbbbbb')]:
        db.execute("""INSERT INTO stock_firmwares(public_token,device_codename,region_code,version_str,android_version,
            package_type,original_official_url,fast_cdn_url,ed25519_signature)
            VALUES (?,?,'Global','OS2','15','recovery_zip',?,?,?)""", (token, code, PRIVATE[3], PRIVATE[3], PRIVATE[2]))
    fw = db.query("SELECT id FROM stock_firmwares WHERE device_codename='marble'")[0]['id']
    db.execute("""INSERT INTO extracted_boot_images(public_token,firmware_id,device_codename,version_str,partition_name,
        variant_type,file_name,file_size_bytes,sha256_hash,gh_private_repo,gh_release_tag,gh_asset_id,ed25519_signature)
        VALUES ('bt_aaaaaaaaaa',?,'marble','OS2','boot','stock','PRIVATE_ASSET_999',1572864,?,?,'private-tag',999,?)""",
        (fw, 'a'*64, PRIVATE[1], PRIVATE[2]))
    return db


def test_jumper_deterministic_hash_and_zero_leak(tmp_path):
    db = fixture(tmp_path)
    try:
        out = tmp_path / 'out'
        a = generate_jumper_markdown_tree(db, out)
        (out / 'devices/stale.md').write_text('old')
        b = generate_jumper_markdown_tree(db, out)
        assert a == b
        assert a['devices_rendered'] == 2 and a['skipped_empty_devices'] == 1
        assert a['total_files_written'] == 3
        files = sorted(out.rglob('*.md'), key=lambda p: p.relative_to(out).as_posix())
        assert a['tree_sha256'] == hashlib.sha256(b''.join(p.read_bytes() for p in files)).hexdigest()
        text = '\n'.join(p.read_text() for p in files)
        for secret in PRIVATE + ['gh_asset_id', 'gh_private_repo', 'fast_cdn_url', 'ed25519_signature', '__global__']:
            assert secret not in text
        assert '`aaaaaaaaaaaa...`' in text and '1.5 MB' in text
        assert '[Download](https://findmyrom.com/verify/fw_bbbbbbbbbb)' in text
        assert not (out / 'devices/stale.md').exists()
        assert not list(out.rglob('*.img'))
        for n in range(65):
            db.execute("""INSERT INTO stock_firmwares(public_token,device_codename,region_code,version_str,android_version,
                package_type,original_official_url,fast_cdn_url,ed25519_signature) VALUES (?,'marble','Global',?,'15','recovery_zip','x','x','x')""",
                ('fw_'+str(n).zfill(10), f'Z{n:03}'))
        generate_jumper_markdown_tree(db, out)
        page=(out/'devices/marble.md').read_text()
        assert 'Z064' in page and 'Z015' in page and 'Z014' not in page
        assert all(p.stat().st_size <= 512000 for p in files)
        with pytest.raises(ValueError):
            generate_jumper_markdown_tree(db, out, 'https://findmyrom.com/?secret=bad')
        db.execute("DELETE FROM extracted_boot_images")
        db.execute("DELETE FROM stock_firmwares WHERE device_codename='alioth'")
        generate_jumper_markdown_tree(db,out)
        assert not (out/'devices/alioth.md').exists()
    finally:
        db.close()


def test_jumper_markdown_table_pipe_and_newline_escaping(tmp_path):
    db = fixture(tmp_path)
    try:
        db.execute("UPDATE devices SET marketing_name=? WHERE codename='marble'", ('Phone | A\n<script>& [link](https://evil.test)',))
        db.execute("UPDATE extracted_boot_images SET version_str=?,variant_type=?", ('OS|1\nnew', 'stock|x'))
        generate_jumper_markdown_tree(db, tmp_path/'out')
        page=(tmp_path/'out/devices/marble.md').read_text()
        assert 'Phone \\| A&lt;script&gt;&amp;' in page
        assert 'OS\\|1new' in page and 'stock\\|x' in page
        assert '<script>' not in page and '\\[link\\]' in page
        assert '| Firmware Version | Partition | Variant | Size | SHA-256 (Prefix) | Download |' in page
    finally:
        db.close()


def test_jumper_cli_execution(tmp_path):
    env=dict(os.environ, LOCAL_SQLITE_PATH=str(tmp_path/'cli.db'), SIGN_PRIVATE_KEY_HEX='11'*32)
    ingest=subprocess.run([sys.executable,'-m','fmr_ingest_stock.cli','--mode','all','--devices-source','tests/fixtures/sp02_devices_sample.yaml','--firmwares-source','tests/fixtures/sp02_firmwares_sample.yml'],cwd=ROOT,env=env,capture_output=True,text=True)
    assert ingest.returncode==0, ingest.stderr
    summaries=[]
    for name in ['one','two']:
        result=subprocess.run([sys.executable,'-m','fmr_extractor.jumper_sync','--output-dir',str(tmp_path/name)],cwd=ROOT,env=env,capture_output=True,text=True)
        assert result.returncode==0,result.stderr
        summary=json.loads(result.stdout.splitlines()[-1]);summaries.append(summary)
        assert summary['devices_rendered']==2 and summary['skipped_empty_devices']==2 and summary['total_files_written']==3
        for file in ['README.md','devices/marble.md','devices/alioth.md']:
            assert (tmp_path/name/file).is_file()
    assert summaries[0]==summaries[1]


def test_telegram_registration_is_outside_worker_and_never_sends_messages():
    from fmr_extractor.telegram_setup import configure
    calls=[]
    class Response:
        def raise_for_status(self): pass
        def json(self): return {'ok':True}
    class Client:
        def post(self,url,**kwargs):
            calls.append((url,kwargs['json']))
            return Response()
    result=configure(Client(),'123:synthetic_bot_token','synthetic_webhook_secret','https://findmyrom.com')
    assert result['webhook_url']=='https://findmyrom.com/api/tg-webhook'
    assert [url.rsplit('/',1)[-1] for url,_ in calls]==['setWebhook','setMyCommands']
    assert calls[0][1]['secret_token']=='synthetic_webhook_secret'
    assert calls[0][1]['allowed_updates']==['message','inline_query']
    assert calls[0][1]['drop_pending_updates'] is False
    assert 'synthetic_bot_token' not in json.dumps(result)


def test_formal_metadata_labels_reach_markdown_without_rekeying(tmp_path):
    from fmr_core.firmware_metadata_schema import ensure
    db=fixture(tmp_path)
    try:
        ensure(db,apply=True)
        db.execute("UPDATE stock_firmwares SET version_str='Verizon',region_code='NDE63X' WHERE public_token='fw_aaaaaaaaaa'")
        db.execute('INSERT INTO firmware_metadata(public_token,metadata_json,content_hash) VALUES (?,?,?)',('fw_aaaaaaaaaa',json.dumps({'schema':1,'display_version':'NDE63X','region':'Verizon','raw_version':'7.1.0 (Verizon, NDE63X, Nov 2016)','sources':{'catalogue':'private-source-not-exported'}}),'hash'))
        generate_jumper_markdown_tree(db,tmp_path/'formal')
        page=(tmp_path/'formal/devices/marble.md').read_text()
        assert '| NDE63X | Verizon / marble |' in page
        assert '[Download](https://findmyrom.com/verify/fw_aaaaaaaaaa)' in page
        assert 'private-source-not-exported' not in page
        row=db.query("SELECT version_str,region_code FROM stock_firmwares WHERE public_token='fw_aaaaaaaaaa'")[0]
        assert row=={'version_str':'Verizon','region_code':'NDE63X'}
    finally:db.close()
