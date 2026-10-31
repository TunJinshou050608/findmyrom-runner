"""Offline acceptance tests for SP02's ingestion and cryptographic contracts."""

import json
import math
from pathlib import Path
import re
import time
from unittest.mock import patch

import pytest
import yaml
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from fmr_core.crypto import generate_keypair
from fmr_core.d1_client import D1Client
from fmr_ingest_stock import rewrite_to_fast_cdn, sync_devices, sync_firmwares
from fmr_ingest_stock.cdn_rewriter import FAST_HOST, MI_HOSTS
from fmr_ingest_stock.common import load_source

FIXTURES = Path(__file__).parent / "fixtures"
DEVICES = str(FIXTURES / "sp02_devices_sample.yaml")
FIRMWARES = str(FIXTURES / "sp02_firmwares_sample.yml")


@pytest.fixture
def db(tmp_path, monkeypatch):
    key, _ = generate_keypair()
    monkeypatch.setenv("SIGN_PRIVATE_KEY_HEX", key)
    client = D1Client(local_sqlite_path=str(tmp_path / "test.db"))
    client.execute_script(
        Path(__file__).parents[1].joinpath("fmr_core/schema.sql").read_text()
    )
    yield client
    client.close()


def write_yaml(tmp_path, rows, name="source.yml"):
    path = tmp_path / name
    path.write_text(yaml.safe_dump(rows))
    return str(path)


def assert_signatures(db, monkeypatch):
    import os

    pub = Ed25519PrivateKey.from_private_bytes(
        bytes.fromhex(os.environ["SIGN_PRIVATE_KEY_HEX"])
    ).public_key()
    for row in db.query("SELECT * FROM stock_firmwares"):
        assert re.fullmatch(r"fw_[A-Za-z0-9_-]{10,13}", row["public_token"])
        payload = f"v1|fw|{row['public_token']}|{row['fast_cdn_url']}".encode()
        pub.verify(bytes.fromhex(row["ed25519_signature"]), payload)


def test_cdn_rewriter_positive_and_ssrf_negative_cases():
    for host in MI_HOSTS:
        assert (
            rewrite_to_fast_cdn("xiaomi", f"http://{host.upper()}/a.ZIP?t=1#test")
            == f"https://{FAST_HOST}/a.ZIP?t=1#test"
        )
    assert (
        rewrite_to_fast_cdn("sony", " https://sony.com/a.zip?x=2 ")
        == "https://sony.com/a.zip?x=2"
    )
    assert (
        rewrite_to_fast_cdn("other", "https://8.8.8.8/a.zip") == "https://8.8.8.8/a.zip"
    )
    for url in [
        "file:///a.zip",
        "ftp://sony.com/a.zip",
        "https://localhost/a.zip",
        "http://127.0.0.1/a.zip",
        "http://10.0.0.1/a.zip",
        "http://169.254.169.254/a.zip",
        "http://[::1]/a.zip",
        "http://224.0.0.1/a.zip",
        "http://192.0.2.1/a.zip",
        "https://user:pass@sony.com/a.zip",
        "https://sony.com:8080/a.zip",
        "https://sony.com:wrong/a.zip",
        "https://so ny.com/a.zip",
        "https://so\nny.com/a.zip",
        "https://[bad/a.zip",
        "//sony.com/a.zip",
    ]:
        with pytest.raises(ValueError):
            rewrite_to_fast_cdn("xiaomi", url)


def test_device_sync_fixture_and_brand_mapping(db, tmp_path):
    assert sync_devices(db, DEVICES) == dict(
        brands_upserted=7, devices_upserted=4, devices_rejected=1
    )
    rows = {r["codename"]: r for r in db.query("SELECT * FROM devices")}
    assert len(db.query("SELECT * FROM brands")) == 7
    assert rows["marble"]["marketing_name"] == "Redmi Note 12 Turbo CN"
    assert rows["pdx234"]["brand_id"] == "sony"
    assert rows["phone2"]["brand_id"] == "other"
    assert rows["alioth"]["boot_partition_type"] == "boot"
    assert rows["__global__"]["brand_id"] == "other"
    db.execute(
        "UPDATE devices SET boot_partition_type='boot',soc_name='measured',model_numbers_json='[\"old\"]' WHERE codename='marble'"
    )
    path = write_yaml(
        tmp_path,
        [
            dict(codename="marble"),
            dict(codename="with_under", oem="One Plus"),
            dict(codename="pixel9", brand="pixel"),
        ],
    )
    sync_devices(db, path)
    row = db.query("SELECT * FROM devices WHERE codename='marble'")[0]
    assert row["boot_partition_type"] == "boot"
    assert row["marketing_name"] == "Redmi Note 12 Turbo CN"
    assert row["soc_name"] == "measured" and json.loads(row["model_numbers_json"]) == [
        "old"
    ]
    assert (
        db.query("SELECT brand_id FROM devices WHERE codename='with_under'")[0][
            "brand_id"
        ]
        == "oneplus"
    )


def test_firmware_sync_fixture_and_real_signature_verification(
    db, monkeypatch, tmp_path
):
    assert sync_firmwares(db, FIRMWARES) == dict(
        inserted_or_updated=5,
        firmwares_rejected=4,
        pending_count=3,
        skipped_count=2,
        status="completed",
    )
    before = db.query("SELECT public_token FROM stock_firmwares ORDER BY id")
    assert_signatures(db, monkeypatch)
    sync_firmwares(db, FIRMWARES)
    assert db.query("SELECT public_token FROM stock_firmwares ORDER BY id") == before
    rows = yaml.safe_load(Path(FIRMWARES).read_text())[:1]
    rows[0].update(
        link="https://bigota.d.miui.com/new.zip?t=2",
        size=1073741824,
        sha256="A" * 64,
        date="2026-10-31",
    )
    sync_firmwares(db, write_yaml(tmp_path, rows))
    row = db.query("SELECT * FROM stock_firmwares WHERE region_code='Global'")[0]
    assert (
        row["public_token"] == before[0]["public_token"]
        and row["file_size_str"] == "1.00 GB"
    )
    assert row["md5_or_sha256"] == "a" * 64 and row["published_date"] == "2026-10-31"
    assert_signatures(db, monkeypatch)


def test_state_machine_sm04_to_sm09_transitions(db, monkeypatch):
    sync_firmwares(db, FIRMWARES)
    for protected in ["done", "processing", "failed"]:
        db.execute(
            "UPDATE stock_firmwares SET extract_status=?,extract_retries=2,extract_error=?",
            (protected, "keep"),
        )
        sync_firmwares(db, FIRMWARES, priority_codenames=set())
        assert {
            r["extract_status"]
            for r in db.query("SELECT extract_status FROM stock_firmwares")
        } == {protected}
        assert all(
            r["extract_retries"] == 2 and r["extract_error"] == "keep"
            for r in db.query("SELECT * FROM stock_firmwares")
        )
    db.execute(
        "UPDATE stock_firmwares SET extract_status='pending' WHERE package_type='recovery_zip'"
    )
    db.execute(
        "UPDATE stock_firmwares SET extract_status='skipped' WHERE package_type='fastboot_tgz'"
    )
    sync_firmwares(db, FIRMWARES, priority_codenames={"marble"})
    assert (
        db.query(
            "SELECT extract_status FROM stock_firmwares WHERE device_codename='alioth' AND package_type='recovery_zip'"
        )[0]["extract_status"]
        == "skipped"
    )
    sync_firmwares(db, FIRMWARES)
    assert (
        sum(
            r["extract_status"] == "pending"
            for r in db.query("SELECT * FROM stock_firmwares")
        )
        == 3
    )
    assert_signatures(db, monkeypatch)


def test_incremental_cursor_skips_unchanged_source(db, tmp_path):
    sync_firmwares(db, FIRMWARES)
    with (
        patch.object(db, "execute", wraps=db.execute) as execute,
        patch.object(db, "execute_batch", wraps=db.execute_batch) as batch,
    ):
        assert (
            sync_firmwares(db, FIRMWARES, incremental=True)["status"]
            == "skipped_unchanged"
        )
        assert execute.call_count == batch.call_count == 0
    rows = yaml.safe_load(Path(FIRMWARES).read_text())[:1]
    path = write_yaml(tmp_path, rows)
    sync_firmwares(db, path, incremental=True)
    rows[0]["android"] = "15"
    write_yaml(tmp_path, rows)
    assert sync_firmwares(db, path, incremental=True)["status"] == "completed"
    cursor = db.query("SELECT * FROM crawler_sources WHERE source_target=?", (path,))[0]
    rows[0]["android"] = "16"
    write_yaml(tmp_path, rows)
    with patch.object(db, "execute_batch", side_effect=RuntimeError("DB failure")):
        with pytest.raises(RuntimeError):
            sync_firmwares(db, path, incremental=True)
    assert (
        db.query("SELECT * FROM crawler_sources WHERE source_target=?", (path,))[0]
        == cursor
    )


def test_batch_size_call_count_and_performance(db, tmp_path):
    rows = [
        dict(
            codename="marble",
            version=f"OS1.{i}",
            link=f"https://bigota.d.miui.com/{i}.zip",
        )
        for i in range(450)
    ]
    path = write_yaml(tmp_path, rows)
    started = time.monotonic()
    with patch.object(db, "execute_batch", wraps=db.execute_batch) as batch:
        stats = sync_firmwares(db, path, batch_size=200)
        calls = batch.call_count
    assert stats["inserted_or_updated"] == 450
    assert calls <= math.ceil(450 / 200) * 2
    assert time.monotonic() - started <= 5


def test_missing_or_invalid_private_key_fails_fast(db, monkeypatch):
    for key in ["", "ab" * 31, "zz" * 32, "AB" * 32]:
        monkeypatch.setenv("SIGN_PRIVATE_KEY_HEX", key)
        with (
            patch.object(db, "execute") as execute,
            patch.object(db, "execute_batch") as batch,
        ):
            for sync, source in [(sync_devices, DEVICES), (sync_firmwares, FIRMWARES)]:
                with pytest.raises(ValueError):
                    sync(db, source)
            assert execute.call_count == batch.call_count == 0


def test_invalid_sources_do_not_write(db, tmp_path):
    for content in ["key: value", "[broken", "!!python/object:object {}"]:
        source = tmp_path / "invalid.yml"
        source.write_text(content)
        with (
            patch.object(db, "execute") as execute,
            patch.object(db, "execute_batch") as batch,
        ):
            with pytest.raises(ValueError):
                sync_firmwares(db, str(source))
            assert execute.call_count == batch.call_count == 0
    with patch("fmr_ingest_stock.common.MAX_SOURCE_BYTES", 2):
        with pytest.raises(ValueError):
            load_source(FIRMWARES)


def test_execute_batch_atomic_rollback(db):
    with pytest.raises(Exception):
        db.execute_batch(
            [
                (
                    "INSERT INTO brands(id,name,bl_unlock_status) VALUES ('test','Test','easy')",
                    (),
                ),
                ("INSERT INTO missing_table VALUES (1)", ()),
            ]
        )
    assert db.query("SELECT * FROM brands WHERE id='test'") == []


def test_legacy_schema_signature_backfill(db, monkeypatch):
    # SQLite supports DROP COLUMN, allowing the original pre-signature schema.
    sync_firmwares(db, FIRMWARES)
    db.execute("ALTER TABLE stock_firmwares DROP COLUMN ed25519_signature")
    sync_devices(db, DEVICES)
    assert_signatures(db, monkeypatch)


def test_remote_metadata_retry_and_size_limits(db, monkeypatch):
    import httpx
    from contextlib import contextmanager
    from fmr_ingest_stock import common

    outcomes = []
    attempts = []

    @contextmanager
    def stream(method, url, **kwargs):
        attempts.append(kwargs)
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        yield outcome

    def response(status, content=b"[]"):
        return httpx.Response(
            status,
            content=content,
            request=httpx.Request("GET", "https://metadata.example.com/devices.yml"),
        )

    monkeypatch.setattr(common.httpx, "stream", stream)
    monkeypatch.setattr(common.time, "sleep", lambda _: None)
    outcomes[:] = [response(503), httpx.ReadTimeout("timeout"), response(200)]
    assert load_source("https://metadata.example.com/devices.yml")[0] == []
    assert len(attempts) == 3 and all(
        a["timeout"] == 30 and not a["follow_redirects"] for a in attempts
    )
    attempts.clear()
    outcomes[:] = [response(404)]
    with patch.object(db, "execute") as execute:
        with pytest.raises(httpx.HTTPStatusError):
            sync_devices(db, "https://metadata.example.com/devices.yml")
        assert execute.call_count == 0 and len(attempts) == 1
    outcomes[:] = [response(503), response(503), response(503)]
    with pytest.raises(httpx.HTTPStatusError):
        load_source("https://metadata.example.com/devices.yml")
    outcomes[:] = [response(200, b"[]   ")]
    monkeypatch.setattr(common, "MAX_SOURCE_BYTES", 2)
    with pytest.raises(ValueError):
        load_source("https://metadata.example.com/devices.yml")


def test_concurrent_insert_preserves_signature(db, tmp_path, monkeypatch):
    from fmr_ingest_stock.firmware_sync import FIELDS, FIRMWARE_SQL, normalize
    from fmr_core.crypto import generate_public_token, sign_firmware
    import os

    original = db.execute_batch
    inserted = False

    def racing_batch(statements):
        nonlocal inserted
        if not inserted and statements[0][0] == FIRMWARE_SQL:
            inserted = True
            row = normalize(yaml.safe_load(Path(FIRMWARES).read_text())[0], "xiaomi")
            token = generate_public_token("fw")
            row.update(
                public_token=token,
                extract_status="processing",
                ed25519_signature=sign_firmware(
                    token, row["fast_cdn_url"], os.environ["SIGN_PRIVATE_KEY_HEX"]
                ),
            )
            original([(FIRMWARE_SQL, tuple(row[f] for f in FIELDS))])
        return original(statements)

    with patch.object(db, "execute_batch", side_effect=racing_batch):
        with pytest.raises(RuntimeError, match="Concurrent"):
            sync_firmwares(db, FIRMWARES)
    assert_signatures(db, monkeypatch)
    assert db.query("SELECT * FROM crawler_sources") == []
    assert sync_firmwares(db, FIRMWARES)["inserted_or_updated"] == 5
    assert_signatures(db, monkeypatch)


def test_remote_execute_batch_atomic_request_and_errors():
    from unittest.mock import MagicMock
    import requests

    remote = D1Client(cf_account_id="test", cf_database_id="test", cf_api_token="test")
    response = MagicMock()
    response.json.return_value = {
        "success": True,
        "result": [
            {"success": True, "meta": {"changes": 1}},
            {"success": True, "meta": {"changes": 1}},
        ],
    }
    statements = [
        ("INSERT OR IGNORE INTO brands VALUES (?)", ("a",)),
        ("INSERT OR IGNORE INTO brands VALUES (?)", ("b",)),
    ]
    unavailable = requests.exceptions.HTTPError(response=MagicMock(status_code=503))
    with (
        patch.object(
            remote.session, "post", side_effect=[unavailable, response]
        ) as post,
        patch("fmr_core.d1_client.time.sleep"),
    ):
        assert remote.execute_batch(statements)["changes"] == 2
        assert post.call_count == 2
        assert post.call_args.kwargs["json"] == {
            "batch": [{"sql": s, "params": list(p)} for s, p in statements]
        }
    response.json.return_value = {
        "success": True,
        "result": [{"success": False, "error": "failed"}, {"success": True}],
    }
    with patch.object(remote.session, "post", return_value=response):
        with pytest.raises(RuntimeError):
            remote.execute_batch(statements)
    remote.close()


def test_large_device_batch_limits_read_bindings(db, tmp_path):
    rows = [
        dict(
            codename=f"device{i}",
            version="OS1.0",
            link=f"https://bigota.d.miui.com/{i}.zip",
        )
        for i in range(450)
    ]
    source = write_yaml(tmp_path, rows)
    with patch.object(db, "query", wraps=db.query) as query:
        result = sync_firmwares(db, source, batch_size=500)
    assert result["inserted_or_updated"] == 450
    assert all(
        len(call.args[1]) <= 100 for call in query.call_args_list if len(call.args) > 1
    )
    assert len(db.query("SELECT * FROM stock_firmwares")) == 450


def test_remote_read_errors_cannot_advance_cursor():
    from unittest.mock import MagicMock

    remote = D1Client(cf_account_id="test", cf_database_id="test", cf_api_token="test")
    response = MagicMock()
    for results in [[], [{"success": False, "error": "database unavailable"}]]:
        response.json.return_value = {"success": True, "result": results}
        with patch.object(remote.session, "post", return_value=response):
            with pytest.raises(RuntimeError):
                remote.query("SELECT last_cursor_val FROM crawler_sources")
            with pytest.raises(RuntimeError):
                remote.execute(
                    "UPDATE crawler_sources SET last_cursor_val=?", ("test",)
                )
    remote.close()
