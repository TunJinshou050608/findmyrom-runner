from pathlib import Path
"""Real ZIP/Payload bytes, real crypto/DB/compressor/Vault; HTTP only is faked."""

import bz2
import gzip
import hashlib
import io
import json
import re
import struct
import time
import zipfile
from functools import partial

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fmr_core.d1_client import D1Client
from fmr_ingest_stock import sync_firmwares
from fmr_extractor import (
    extract_partitions_via_range,
    validate_and_compress_partition,
    VaultClient,
    run_extraction_batch,
)
from fmr_extractor.range_extractor import RangeNotSupportedError


def image(name="boot", bad=False):
    magic = {
        "boot": b"ANDROID!",
        "init_boot": b"ANDROID!",
        "vendor_boot": b"VNDRBOOT",
        "vbmeta": b"AVB0",
    }[name]
    return (b"INVALID!" if bad else magic).ljust(65536, b"\0")


def zip_bytes(images):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, data in images.items():
            archive.writestr(name, data)
    return output.getvalue()


class Server:
    def __init__(self, rom):
        self.rom = rom
        self.uploads = []
        self.calls = []
        self.conflict = False
        self.auth = False
        self.public = False
        self.full = False

    def __call__(self, request):
        self.calls.append(request)
        if request.url.host == "firmware.example.com":
            if self.full:
                return httpx.Response(
                    200,
                    headers={"Content-Length": str(6 * 1024**3)},
                    stream=httpx.ByteStream(b""),
                )
            start, end = map(
                int,
                re.fullmatch(r"bytes=(\d+)-(\d+)", request.headers["range"]).groups(),
            )
            return httpx.Response(
                206,
                headers={
                    "Content-Range": f"bytes {start}-{end}/{len(self.rom)}",
                    "ETag": '"stable"',
                },
                stream=httpx.ByteStream(self.rom[start : end + 1]),
            )
        assert request.url.host in {"api.github.com", "uploads.github.com"}
        assert request.headers["authorization"] == "Bearer ghp_testtoken123"
        if self.auth:
            return httpx.Response(401)
        if request.url.path == "/repos/owner/vault":
            return httpx.Response(200, json={"private": not self.public})
        if request.url.host == "uploads.github.com":
            self.uploads.append((str(request.url.params["name"]), request.content))
            return httpx.Response(422 if self.conflict else 201, json={"id": 123})
        if request.url.path.endswith("/assets"):
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 123,
                        "name": self.uploads[-1][0],
                        "size": len(self.uploads[-1][1]),
                    }
                ],
            )
        return httpx.Response(200, json={"id": 12})


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("SIGN_PRIVATE_KEY_HEX", "11" * 32)
    monkeypatch.setenv("GH_VAULT_TOKEN", "ghp_testtoken123")
    monkeypatch.setenv("GH_VAULT_REPO", "owner/vault")
    monkeypatch.setenv("FMR_TEST_NO_SLEEP", "1")
    db = D1Client(local_sqlite_path=str(tmp_path / "test.db"))
    source = tmp_path / "firmwares.yml"
    source.write_text(
        json.dumps(
            [
                dict(
                    codename="sample",
                    version="v1",
                    link="https://firmware.example.com/fw.zip",
                )
            ]
        )
    )
    sync_firmwares(db, str(source))
    server = Server(zip_bytes({"boot.img": image()}))
    with httpx.Client(transport=httpx.MockTransport(server)) as client:
        vault = VaultClient("ghp_testtoken123", "owner/vault", client)
        extractor = partial(extract_partitions_via_range, http_client=client)
        yield db, server, vault, extractor, tmp_path
    db.close()


def test_full_extraction_pipeline_and_device_boot_type_calibration(setup):
    db, server, vault, extractor, tmp = setup
    for part in ["boot", "init_boot"]:
        server.rom = zip_bytes(
            {part + ".img": image(part), "vbmeta.img": image("vbmeta")}
        )
        db.execute("UPDATE stock_firmwares SET extract_status='pending'")
        result = run_extraction_batch(db, vault, extractor)
        assert result["succeeded"] == 1 and result["failed"] == 0
        assert (
            db.query(
                "SELECT boot_partition_type FROM devices WHERE codename=?", ("sample",)
            )[0]["boot_partition_type"]
            == part
        )
        rows = db.query("SELECT * FROM extracted_boot_images")
        pub = Ed25519PrivateKey.from_private_bytes(
            bytes.fromhex("11" * 32)
        ).public_key()
        for row in rows:
            payload = f"v1|bt|{row['public_token']}|{row['sha256_hash']}|{row['gh_private_repo']}|{row['gh_asset_id']}"
            pub.verify(bytes.fromhex(row["ed25519_signature"]), payload.encode())
            assert re.fullmatch(r"bt_[A-Za-z0-9_-]{10,13}", row["public_token"])
        blob = server.uploads[-1][1]
        assert gzip.decompress(blob).startswith(b"AVB0")
        assert any(
            r["sha256_hash"] == hashlib.sha256(blob).hexdigest()
            and r["file_size_bytes"] == len(blob)
            for r in rows
        )
    tokens = db.query("SELECT public_token FROM extracted_boot_images ORDER BY id")
    db.execute("UPDATE stock_firmwares SET extract_status='pending'")
    run_extraction_batch(db, vault, extractor)
    assert (
        db.query("SELECT public_token FROM extracted_boot_images ORDER BY id") == tokens
    )


def test_abort_on_200_full_rom_exceeding_250mb_limit(setup):
    db, server, vault, extractor, tmp = setup
    server.full = True
    with pytest.raises(RangeNotSupportedError):
        extractor("https://firmware.example.com/fw.zip", tmp / "direct")
    result = run_extraction_batch(db, vault, extractor)
    assert result["failed"] == 1 and not server.uploads
    assert (
        db.query("SELECT extract_status FROM stock_firmwares")[0]["extract_status"]
        == "failed"
    )


def test_reject_bad_magic_header_and_undersized_image(setup):
    db, server, vault, extractor, tmp = setup
    for blob in [image(bad=True), b"ANDROID!"]:
        server.rom = zip_bytes({"boot.img": blob})
        db.execute(
            "UPDATE stock_firmwares SET extract_status='pending',extract_retries=0"
        )
        assert run_extraction_batch(db, vault, extractor)["failed"] == 1
    assert not server.uploads
    assert db.query("SELECT * FROM extracted_boot_images") == []


def test_stale_processing_lock_recovery_and_max_retries(setup):
    db, server, vault, extractor, tmp = setup
    for retries, expected in [(0, "pending"), (2, "failed")]:
        db.execute(
            "UPDATE stock_firmwares SET extract_status='processing',extract_retries=?,updated_at=?",
            (retries, int(time.time()) - 1801),
        )
        result = run_extraction_batch(db, vault, extractor, dry_run=True)
        row = db.query("SELECT * FROM stock_firmwares")[0]
        assert result["stale_recovered"] == 1 and row["extract_status"] == expected
        assert row["extract_retries"] == retries + 1
    assert not server.calls


def test_github_422_duplicate_asset_conflict_resolution(setup):
    db, server, vault, extractor, tmp = setup
    server.conflict = True
    assert run_extraction_batch(db, vault, extractor)["succeeded"] == 1
    assert server.uploads[0][0].startswith("blob_") and server.uploads[0][0].endswith(
        ".bin"
    )
    assert (
        db.query("SELECT gh_asset_id FROM extracted_boot_images")[0]["gh_asset_id"]
        == 123
    )


def test_github_401_triggers_circuit_breaker_without_retry_penalty(setup):
    db, server, vault, extractor, tmp = setup
    server.auth = True
    result = run_extraction_batch(db, vault, extractor, max_workers=1)
    assert result["circuit_broken"] and result["status"] == "circuit_broken"
    row = db.query("SELECT * FROM stock_firmwares")[0]
    assert row["extract_status"] == "pending" and row["extract_retries"] == 0


def test_workspace_temp_dirs_cleaned_after_success_and_failure(setup, monkeypatch):
    import tempfile

    db, server, vault, extractor, tmp = setup
    monkeypatch.setattr(tempfile, "tempdir", str(tmp))
    for bad in [False, True]:
        server.rom = zip_bytes({"boot.img": image(bad=bad)})
        db.execute("UPDATE stock_firmwares SET extract_status='pending'")
        run_extraction_batch(db, vault, extractor)
        assert list(tmp.glob("fw_*")) == []


def test_deterministic_gzip_and_path_sanitization(tmp_path):
    raw = tmp_path / "boot.img"
    raw.write_bytes(image())
    a = validate_and_compress_partition(raw, "boot", "../../bad", "a/../b")
    data = a["gz_path"].read_bytes()
    b = validate_and_compress_partition(raw, "boot", "../../bad", "a/../b")
    assert (
        data == b["gz_path"].read_bytes()
        and ".." not in a["file_name"]
        and "/" not in a["file_name"]
    )
    assert hashlib.sha256(data).hexdigest() == a["sha256_hash"]


def test_public_vault_is_rejected_before_mutation(setup):
    db, server, vault, extractor, tmp = setup
    server.public = True
    result = run_extraction_batch(db, vault, extractor)
    assert result["circuit_broken"] and not server.uploads
    assert not any(r.method == "POST" for r in server.calls)


def varint(n):
    result = bytearray()
    while n >= 128:
        result.append((n & 127) | 128)
        n >>= 7
    result.append(n)
    return bytes(result)


def proto(number, value):
    return varint(number * 8 + (2 if isinstance(value, bytes) else 0)) + (
        varint(len(value)) + value if isinstance(value, bytes) else varint(value)
    )


def payload_bytes(raw, kind=0):
    compressed = bz2.compress(raw) if kind == 1 else __import__("zstandard").ZstdCompressor().compress(raw) if kind == 14 else raw
    extent = proto(1, 0) + proto(2, len(raw) // 4096)
    operation = (
        proto(1, kind)
        + proto(2, 0)
        + proto(3, len(compressed))
        + proto(6, extent)
        + proto(8, hashlib.sha256(compressed).digest())
    )
    info = proto(1, len(raw)) + proto(2, hashlib.sha256(raw).digest())
    part = proto(1, b"boot") + proto(7, info) + proto(8, operation)
    manifest = proto(3, 4096) + proto(13, part)
    return struct.pack(">4sQQI", b"CrAU", 2, len(manifest), 0) + manifest + compressed


@pytest.mark.parametrize("kind", [0, 1, 14])
def test_real_payload_replacement_operations(setup, kind):
    db, server, vault, extractor, tmp = setup
    server.rom = zip_bytes({"payload.bin": payload_bytes(image(), kind)})
    assert run_extraction_batch(db, vault, extractor)["succeeded"] == 1
    assert gzip.decompress(server.uploads[-1][1]) == image()


def test_cumulative_range_budget(setup):
    db, server, vault, extractor, tmp = setup
    with pytest.raises(RangeNotSupportedError):
        extractor("https://firmware.example.com/fw.zip", tmp / "cap", max_bytes=100)
    assert not server.uploads


def test_optional_patch_hook_keeps_stock_and_versions(setup, monkeypatch):
    from fmr_extractor import patcher

    db, server, vault, extractor, tmp = setup
    monkeypatch.setattr(patcher, "PATCH_HOOKS", {})
    patcher.register_patch_hook("magisk", "test-v1", lambda path: path)
    assert run_extraction_batch(db, vault, extractor)["succeeded"] == 1
    rows = db.query(
        "SELECT variant_type,patch_tool_ver FROM extracted_boot_images ORDER BY variant_type"
    )
    assert rows == [
        {"variant_type": "magisk", "patch_tool_ver": "test-v1"},
        {"variant_type": "stock", "patch_tool_ver": None},
    ]


def test_retryable_failure_exhaustion_and_404(setup):
    db, server, vault, extractor, tmp = setup

    def unavailable(request):
        return httpx.Response(503)

    with httpx.Client(transport=httpx.MockTransport(unavailable)) as client:
        offline = partial(extract_partitions_via_range, http_client=client)
        for attempt in range(3):
            result = run_extraction_batch(db, vault, offline)
            assert result["retried" if attempt < 2 else "failed"] == 1
    assert (
        db.query("SELECT extract_retries FROM stock_firmwares")[0]["extract_retries"]
        == 3
    )
    db.execute("UPDATE stock_firmwares SET extract_status='pending',extract_retries=0")
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(404))
    ) as client:
        assert (
            run_extraction_batch(
                db, vault, partial(extract_partitions_via_range, http_client=client)
            )["failed"]
            == 1
        )


def test_wrong_content_range_and_payload_hash_rejected(setup):
    db, server, vault, extractor, tmp = setup
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                206,
                headers={"Content-Range": "bytes 1-1/20"},
                stream=httpx.ByteStream(b"a"),
            )
        )
    ) as client:
        with pytest.raises(RangeNotSupportedError):
            extract_partitions_via_range(
                "https://firmware.example.com/fw.zip", tmp / "wrong", http_client=client
            )
    payload = payload_bytes(image())
    server.rom = zip_bytes({"payload.bin": payload[:-1] + b"x"})
    assert run_extraction_batch(db, vault, extractor)["failed"] == 1
    assert not server.uploads


def test_go_adapter_uses_bounded_local_payload(setup):
    from fmr_extractor.range_extractor import payload_dumper_executor

    db, server, vault, extractor, tmp = setup
    server.rom = zip_bytes({"payload.bin": payload_bytes(image())})

    def command_runner(argv, **kwargs):
        assert argv[0] == "payload-dumper-go"
        assert not any("https://" in argument for argument in argv)
        assert Path(argv[-1]).read_bytes().startswith(b"CrAU")
        assert kwargs["check"] and kwargs["timeout"] > 0
        output = Path(argv[argv.index("-o") + 1])
        (output / "boot.img").write_bytes(image())

    result = extractor(
        "https://firmware.example.com/fw.zip",
        tmp / "go",
        command_executor=payload_dumper_executor(command_runner),
    )
    assert result["boot"].read_bytes() == image()


def test_patch_tool_version_updates_keep_token(setup, monkeypatch):
    from fmr_extractor import patcher

    db, server, vault, extractor, tmp = setup
    monkeypatch.setattr(patcher, "PATCH_HOOKS", {})
    for version in ["first", "second"]:
        patcher.register_patch_hook("magisk", version, lambda path: path)
        db.execute("UPDATE stock_firmwares SET extract_status='pending'")
        assert run_extraction_batch(db, vault, extractor)["succeeded"] == 1
        current = db.query(
            "SELECT public_token,patch_tool_ver FROM extracted_boot_images WHERE variant_type='magisk'"
        )[0]
        assert current["patch_tool_ver"] == version
        if version == "first":
            token = current["public_token"]
        else:
            assert current["public_token"] == token


@pytest.mark.parametrize('layout', ['zip', 'payload'])
def test_target_inventory_comes_from_the_actual_package(setup, layout):
    db, server, vault, extractor, tmp = setup
    server.rom = zip_bytes({'boot.img': image(), 'vbmeta.img': image('vbmeta')}) if layout == 'zip' else zip_bytes({'payload.bin': payload_bytes(image())})
    inventory = {}
    result = extractor('https://firmware.example.com/fw.zip', tmp / layout, inventory=inventory)
    expected = ['boot','vbmeta'] if layout == 'zip' else ['boot']
    assert inventory == {'format':layout, 'partitions':expected}
    assert set(result) == set(expected)


@pytest.mark.parametrize('compressed', [False, True])
def test_factory_nested_image_zip_uses_ranges_and_refuses_compressed_parent(setup, compressed):
    db, server, vault, extractor, tmp = setup
    nested = zip_bytes({'boot.img': image(), 'vbmeta.img': image('vbmeta')})
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as outer:
        outer.writestr('sample/image-sample-build.zip', nested,
                       compress_type=zipfile.ZIP_DEFLATED if compressed else zipfile.ZIP_STORED)
    server.rom = output.getvalue()
    if compressed:
        with pytest.raises(ValueError, match='forbidden full-parent'):
            extractor('https://firmware.example.com/fw.zip', tmp / 'nested')
    else:
        inventory = {}
        result = extractor('https://firmware.example.com/fw.zip', tmp / 'nested', inventory=inventory)
        assert result['boot'].read_bytes() == image()
        assert result['vbmeta'].read_bytes() == image('vbmeta')
        assert inventory == {'format':'nested_zip','partitions':['boot','vbmeta']}
    assert all('range' in r.headers for r in server.calls if r.url.host=='firmware.example.com')


@pytest.mark.parametrize('extra', [False, True])
def test_zstandard_without_size_and_concatenated_frames(setup, extra):
    import zstandard
    db, server, vault, extractor, tmp = setup
    raw=image()
    compressed=zstandard.ZstdCompressor(write_content_size=False).compress(raw)
    if extra:compressed+=zstandard.ZstdCompressor().compress(raw)
    extent=proto(1,0)+proto(2,len(raw)//4096)
    op=proto(1,14)+proto(2,0)+proto(3,len(compressed))+proto(6,extent)+proto(8,hashlib.sha256(compressed).digest())
    part=proto(1,b'boot')+proto(7,proto(1,len(raw))+proto(2,hashlib.sha256(raw).digest()))+proto(8,op)
    manifest=proto(3,4096)+proto(13,part)
    payload=struct.pack('>4sQQI',b'CrAU',2,len(manifest),0)+manifest+compressed
    server.rom=zip_bytes({'payload.bin':payload})
    if extra:
        with pytest.raises((ValueError,zstandard.ZstdError)):extractor('https://firmware.example.com/fw.zip',tmp/'zstd')
    else:
        result=extractor('https://firmware.example.com/fw.zip',tmp/'zstd')
        assert result['boot'].read_bytes()==raw


@pytest.mark.parametrize('encoding', ['raw','lz4','concatenated','bad_magic','oversize','compressed_parent'])
def test_samsung_stored_ap_tar_extraction_and_rejection(setup,encoding):
    import tarfile
    import lz4.frame
    db, server, vault, extractor, tmp = setup
    raw=image(bad=encoding=='bad_magic')
    if encoding=='oversize':raw=b'A'*(1048576+1)
    compressed=encoding not in ('raw','compressed_parent')
    data=lz4.frame.compress(raw) if compressed else raw
    if encoding=='concatenated':data+=lz4.frame.compress(raw)
    tar=io.BytesIO()
    with tarfile.open(fileobj=tar,mode='w',format=tarfile.USTAR_FORMAT) as ap:
        irrelevant=tarfile.TarInfo('system.img');irrelevant.size=2*1024*1024
        ap.addfile(irrelevant,io.BytesIO(b'X'*irrelevant.size))
        name=('vbmeta' if encoding=='oversize' else 'boot')+'.img'+('.lz4' if compressed else '')
        entry=tarfile.TarInfo(name);entry.size=len(data);ap.addfile(entry,io.BytesIO(data))
    out=io.BytesIO()
    with zipfile.ZipFile(out,'w') as outer:
        outer.writestr('AP_SAMPLE.tar.md5',tar.getvalue(),compress_type=zipfile.ZIP_DEFLATED if encoding=='compressed_parent' else zipfile.ZIP_STORED)
    server.rom=out.getvalue()
    if encoding in ('concatenated','bad_magic','oversize','compressed_parent'):
        with pytest.raises(ValueError):extractor('https://firmware.example.com/fw.zip',tmp/encoding)
    else:
        inventory={}
        result=extractor('https://firmware.example.com/fw.zip',tmp/encoding,inventory=inventory)
        assert result['boot'].read_bytes()==image()
        assert inventory=={'format':'samsung_ap_tar','partitions':['boot']}
        consumed=sum(len(server.rom[int(r.headers['range'].split('=')[1].split('-')[0]):int(r.headers['range'].split('-')[-1])+1]) for r in server.calls if r.url.host=='firmware.example.com')
        assert consumed<1024*1024  # Never read the irrelevant system.img body.
