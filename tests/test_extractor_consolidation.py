"""Tests for fmr_extractor consolidation, Zstd 64MiB window, RemoteZip read-ahead,
deflated nested Factory archives, recovery partition support, and decoupled cdn_rewriter.
"""

from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path
import re
import shutil
import socket
import struct
import subprocess
import sys
import zipfile

import httpx
import pytest
import zstandard as zstd

from fmr_extractor.patcher import LIMITS, validate_and_compress_partition
from fmr_extractor.payload import extract_payload
from fmr_extractor.range_extractor import (
    RangeNotSupportedError,
    RemoteZip,
    extract_partitions_via_range,
)
from fmr_ingest_stock.cdn_rewriter import (
    FAST_HOST,
    MI_HOSTS,
    rewrite_to_fast_cdn,
    validate_official_download_url,
)

RUNNER_ROOT = Path(__file__).resolve().parents[1]


def _encode_varint(value: int) -> bytes:
    out = bytearray()
    while True:
        chunk = value & 0x7F
        value >>= 7
        if value:
            out.append(chunk | 0x80)
        else:
            out.append(chunk)
            return bytes(out)


def _pb_field(number: int, wire: int, payload: bytes | int) -> bytes:
    tag = _encode_varint((number << 3) | wire)
    if wire == 0:
        assert isinstance(payload, int)
        return tag + _encode_varint(payload)
    assert isinstance(payload, bytes)
    if wire == 2:
        return tag + _encode_varint(len(payload)) + payload
    return tag + payload


def _make_partition_image(part: str, size: int = 65536, bad_magic: bool = False) -> bytes:
    magic = b"CORRUPT!" if bad_magic else LIMITS[part][2]
    assert size >= len(magic)
    return magic + (b"\x00" * (size - len(magic)))


def test_lazy_init_decouples_cdn_rewriter_and_extractor_without_fmr_core(tmp_path: Path):
    isolated = tmp_path / "isolated_app"
    shutil.copytree(
        RUNNER_ROOT / "fmr_ingest_stock",
        isolated / "fmr_ingest_stock",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    shutil.copytree(
        RUNNER_ROOT / "fmr_extractor",
        isolated / "fmr_extractor",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    code = (
        "import sys\n"
        f"runner_root = {str(RUNNER_ROOT)!r}\n"
        f"sys.path[:] = [{str(isolated)!r}] + [p for p in sys.path if p not in ('', runner_root)]\n"
        "from fmr_ingest_stock.cdn_rewriter import rewrite_to_fast_cdn, validate_official_download_url\n"
        "from fmr_ingest_stock import rewrite_to_fast_cdn as lazy_rewrite\n"
        "from fmr_extractor.range_extractor import RemoteZip, extract_partitions_via_range\n"
        "from fmr_extractor import extract_partitions_via_range as lazy_extract\n"
        "assert 'fmr_core' not in sys.modules\n"
        "assert 'fmr_ingest_stock.device_sync' not in sys.modules\n"
        "assert lazy_rewrite is rewrite_to_fast_cdn\n"
        "assert lazy_extract is extract_partitions_via_range\n"
        "out = validate_official_download_url('xiaomi', 'http://bigota.d.miui.com/V1/fw.zip')\n"
        f"assert out == 'https://{FAST_HOST}/V1/fw.zip'\n"
    )
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    proc = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0, proc.stderr


def test_cdn_rewriter_https_and_ssrf_hardening():
    for host in MI_HOSTS:
        assert (
            rewrite_to_fast_cdn("xiaomi", f"http://{host}/rom.zip")
            == f"https://{FAST_HOST}/rom.zip"
        )
    assert (
        rewrite_to_fast_cdn("sony", "http://software.sonymobile.com/rom.zip")
        == "https://software.sonymobile.com/rom.zip"
    )
    for bad_url in [
        "http://dl.google.com/rom.zip",
        "http://firmware.example.com/rom.zip",
        "http://8.8.8.8/rom.zip",
        "https://100.64.0.1/rom.zip",
        "https://100.100.100.200/rom.zip",
        "https://0.0.0.0/rom.zip",
        "https://[::ffff:127.0.0.1]/rom.zip",
        "https://[::ffff:100.64.0.1]/rom.zip",
        "https://localhost/rom.zip",
        "https://sub.localhost/rom.zip",
        "https://nas.local/rom.zip",
        "https://metadata.internal/rom.zip",
        "https://sony.com:80/rom.zip",
    ]:
        with pytest.raises(ValueError):
            rewrite_to_fast_cdn("xiaomi", bad_url)


def test_validate_official_download_url_allowlist_upgrade_and_dns_ssrf(monkeypatch):
    # Legacy HTTP auto-upgrade works ONLY for d.miui.com, sonymobile.com, sony.net
    assert (
        validate_official_download_url("xiaomi", "http://hugeota.d.miui.com/os/fw.zip")
        == f"https://{FAST_HOST}/os/fw.zip"
    )
    assert (
        validate_official_download_url("sony", "http://software.sonymobile.com/xperia.zip")
        == "https://software.sonymobile.com/xperia.zip"
    )
    assert (
        validate_official_download_url("sony", "http://dl.sony.net/xperia.zip")
        == "https://dl.sony.net/xperia.zip"
    )
    assert (
        validate_official_download_url("google", "https://dl.google.com/dl/android/aosp/img.zip")
        == "https://dl.google.com/dl/android/aosp/img.zip"
    )

    # HTTP rejected for all non-legacy official hosts
    for non_legacy_http in [
        ("google", "http://dl.google.com/dl/android/img.zip"),
        ("oneplus", "http://otafsg1.h2os.allawnofs.com/ota.zip"),
        ("samsung", "http://fota-cloud-dn.ospserver.net/fw.zip"),
    ]:
        with pytest.raises(ValueError):
            validate_official_download_url(*non_legacy_http)

    # Strict aliyuncs.com handling: never wildcard aliyuncs.com!
    assert (
        validate_official_download_url("xiaomi", f"https://{FAST_HOST}/V1/fw.zip")
        == f"https://{FAST_HOST}/V1/fw.zip"
    )
    for untrusted in [
        ("xiaomi", "https://evil.aliyuncs.com/V1/fw.zip"),
        ("xiaomi", "https://attacker.oss-ap-southeast-1.aliyuncs.com/V1/fw.zip"),
        ("xiaomi", f"https://sub.{FAST_HOST}/V1/fw.zip"),
        ("xiaomi", f"https://{FAST_HOST}.evil.example/V1/fw.zip"),
        ("sony", "http://bigota.d.miui.com/V1/fw.zip"),
        ("google", "https://evil.example.com/fw.zip"),
        ("motorola", "https://mirrors.loli" + "net.com/firmware/moto/rom.zip"),
        ("", "https://mirrors.loli" + "net.com/firmware/moto/rom.zip"),
        ("samsung", "https://fwat" + "las.com/download/fw.zip"),
        ("", "https://fwat" + "las.com/download/fw.zip"),
        ("xiaomi", "https://xmfirmware" + "updater.com/miui/rom.zip"),
        ("", "https://xmfirmware" + "updater.com/miui/rom.zip"),
        ("asus", "https://igoreis" + "berg.com/asus/fw.zip"),
        ("", "https://igoreis" + "berg.com/asus/fw.zip"),
        ("oneplus", "https://otacheck" + "er.com/ota/rom.zip"),
        ("", "https://otacheck" + "er.com/ota/rom.zip"),
        ("google", "https://roms.danielsprin" + "ger.at/pixel/rom.zip"),
        ("", "https://roms.danielsprin" + "ger.at/pixel/rom.zip"),
    ]:
        with pytest.raises(ValueError):
            validate_official_download_url(*untrusted)

    #Wildcard aliyuncs.com passed via custom allowed_hosts is still refused
    with pytest.raises(ValueError):
        validate_official_download_url(
            "xiaomi",
            "https://evil.aliyuncs.com/fw.zip",
            allowed_hosts=("aliyuncs.com",),
        )

    # DNS resolution SSRF / DNS-rebinding checks
    def fake_getaddrinfo_cgnat(host, port, type=0):
        assert host == "dl.google.com" and port == 443 and type == socket.SOCK_STREAM
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("100.64.10.20", 443))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo_cgnat)
    with pytest.raises(ValueError, match="non-global IP"):
        validate_official_download_url(
            "google", "https://dl.google.com/ota.zip", resolve_dns=True
        )

    def fake_getaddrinfo_loopback(host, port, type=0):
        return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", 443, 0, 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo_loopback)
    with pytest.raises(ValueError, match="non-global IP"):
        validate_official_download_url(
            "google", "https://dl.google.com/ota.zip", resolve_dns=True
        )

    def fake_getaddrinfo_public(host, port, type=0):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("142.250.190.78", 443))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo_public)
    assert (
        validate_official_download_url(
            "google", "https://dl.google.com/ota.zip", resolve_dns=True
        )
        == "https://dl.google.com/ota.zip"
    )


def test_payload_zstd_large_window_and_streaming_sha256(tmp_path: Path):
    # Create a 2 MiB boot image where identical patterns are > 512 KiB apart,
    # compressed with an explicit 8 MiB Zstd window (window_log=23 >> 64 KiB).
    pattern = os.urandom(32768)
    filler = bytes((i * 31) & 0xFF for i in range(1024 * 1024))
    raw_boot = (
        b"ANDROID!"
        + pattern
        + filler
        + pattern
        + b"\x00" * (2 * 1024 * 1024 - 8 - 2 * len(pattern) - len(filler))
    )
    assert len(raw_boot) == 2 * 1024 * 1024

    cparams = zstd.ZstdCompressionParameters.from_level(
        3, window_log=23, write_content_size=1
    )
    compressed_op = zstd.ZstdCompressor(compression_params=cparams).compress(raw_boot)
    frame_params = zstd.get_frame_parameters(compressed_op)
    assert frame_params.window_size > 64 * 1024  # Exceeds the old buggy 64 KiB cap!

    extent = _pb_field(1, 0, 0) + _pb_field(2, 0, len(raw_boot) // 4096)
    install_op = (
        _pb_field(1, 0, 14)  # ZSTD
        + _pb_field(2, 0, 0)
        + _pb_field(3, 0, len(compressed_op))
        + _pb_field(6, 2, extent)
        + _pb_field(8, 2, hashlib.sha256(compressed_op).digest())
    )
    part_info = _pb_field(1, 0, len(raw_boot)) + _pb_field(
        2, 2, hashlib.sha256(raw_boot).digest()
    )
    part_update = (
        _pb_field(1, 2, b"boot")
        + _pb_field(7, 2, part_info)
        + _pb_field(8, 2, install_op)
    )
    manifest = _pb_field(3, 0, 4096) + _pb_field(13, 2, part_update)
    payload_bytes = (
        struct.pack(">4sQQI", b"CrAU", 2, len(manifest), 0) + manifest + compressed_op
    )

    inventory: dict[str, object] = {}
    extracted = extract_payload(
        io.BytesIO(payload_bytes),
        0,
        len(payload_bytes),
        tmp_path,
        targets=("boot",),
        inventory=inventory,
    )
    assert extracted["boot"].read_bytes() == raw_boot
    assert inventory == {"format": "payload", "partitions": ["boot"]}


def test_remote_zip_1mib_readahead_utf8_encoding_and_seek_bounds():
    blob = bytes(i & 0xFF for i in range(2 * 1024 * 1024))
    range_calls: list[tuple[int, int]] = []
    encoding_header = {"value": "UTF-8"}

    def handler(request: httpx.Request) -> httpx.Response:
        m = re.fullmatch(r"bytes=(\d+)-(\d+)", request.headers["range"])
        assert m is not None
        start, end = int(m.group(1)), int(m.group(2))
        range_calls.append((start, end))
        return httpx.Response(
            206,
            headers={
                "Content-Range": f"bytes {start}-{end}/{len(blob)}",
                "Content-Encoding": encoding_header["value"],
                "ETag": '"v1"',
            },
            stream=httpx.ByteStream(blob[start : end + 1]),
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        rz = RemoteZip("https://dl.google.com/ota.zip", client, max_bytes=4 * 1024 * 1024)
        assert range_calls == [(0, 0)]
        assert rz.bytes_downloaded == 1

        # Multiple sequential small reads coalesce into a single 1 MiB Range request!
        range_calls.clear()
        chunks = [rz.read(128) for _ in range(50)]
        assert b"".join(chunks) == blob[: 128 * 50]
        assert len(range_calls) == 1
        assert range_calls[0] == (1, 1024 * 1024)
        assert rz.bytes_downloaded == 1 + 1024 * 1024

        # Seek bounds validation
        with pytest.raises(ValueError):
            rz.seek(-1, 0)
        with pytest.raises(ValueError):
            rz.seek(0, 99)
        with pytest.raises(ValueError):
            rz.seek(len(blob) + 1, 0)

        # Reject real content compression like gzip
        encoding_header["value"] = "gzip"
        rz.seek(1500 * 1024)
        with pytest.raises(RangeNotSupportedError, match="Encoded Range response"):
            rz.read(64)


def test_deflated_nested_factory_zip_magic_validation_and_recovery_partition(tmp_path: Path):
    boot_img = _make_partition_image("boot", 65536)
    recovery_img = _make_partition_image("recovery", 131072)
    vbmeta_img = _make_partition_image("vbmeta", 4096)

    nested_buf = io.BytesIO()
    with zipfile.ZipFile(nested_buf, "w", compression=zipfile.ZIP_DEFLATED) as inner:
        inner.writestr("android-info.txt", b"require board=husky\n")
        inner.writestr("boot.img", boot_img)
        inner.writestr("system.img", b"\x55" * 524288)
        inner.writestr("recovery.img", recovery_img)
        inner.writestr("vbmeta.img", vbmeta_img, compress_type=zipfile.ZIP_STORED)

    outer_buf = io.BytesIO()
    with zipfile.ZipFile(outer_buf, "w", compression=zipfile.ZIP_DEFLATED) as outer:
        outer.writestr("husky/flash-all.sh", b"#!/bin/sh\n")
        outer.writestr("husky/image-husky-ud1a.zip", nested_buf.getvalue())
    rom_bytes = outer_buf.getvalue()

    def make_client(data: bytes) -> httpx.Client:
        def handler(request: httpx.Request) -> httpx.Response:
            m = re.fullmatch(r"bytes=(\d+)-(\d+)", request.headers["range"])
            assert m is not None
            start, end = int(m.group(1)), int(m.group(2))
            return httpx.Response(
                206,
                headers={"Content-Range": f"bytes {start}-{end}/{len(data)}"},
                stream=httpx.ByteStream(data[start : end + 1]),
            )

        return httpx.Client(transport=httpx.MockTransport(handler))

    with make_client(rom_bytes) as client:
        inventory: dict[str, object] = {}
        out_dir = tmp_path / "extracted"
        extracted = extract_partitions_via_range(
            "https://dl.google.com/husky-factory.zip",
            out_dir,
            target_partitions=("boot", "recovery", "vbmeta"),
            http_client=client,
            inventory=inventory,
        )
        assert set(extracted) == {"boot", "recovery", "vbmeta"}
        assert extracted["boot"].read_bytes() == boot_img
        assert extracted["recovery"].read_bytes() == recovery_img
        assert extracted["vbmeta"].read_bytes() == vbmeta_img
        assert inventory == {
            "format": "nested_zip",
            "partitions": ["boot", "recovery", "vbmeta"],
        }

        # Validate recovery compression support in patcher.py
        compressed_meta = validate_and_compress_partition(
            extracted["recovery"], "recovery", "husky", "UD1A", out_dir=tmp_path / "gz"
        )
        assert compressed_meta["partition_name"] == "recovery"
        assert compressed_meta["gz_path"].is_file()

        # Tight max_bytes rejects deflated nested archive when compress_size > remaining budget
        with pytest.raises(RangeNotSupportedError):
            extract_partitions_via_range(
                "https://dl.google.com/husky-factory.zip",
                tmp_path / "tight_budget",
                target_partitions=("boot",),
                max_bytes=512,
                http_client=client,
            )

    # Direct ZIP with invalid partition magic deletes invalid file and raises ValueError
    bad_direct_buf = io.BytesIO()
    with zipfile.ZipFile(bad_direct_buf, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr("boot.img", _make_partition_image("boot", 65536, bad_magic=True))
    bad_direct_dir = tmp_path / "bad_direct"
    with make_client(bad_direct_buf.getvalue()) as client:
        with pytest.raises(ValueError, match="Invalid partition image magic or size"):
            extract_partitions_via_range(
                "https://dl.google.com/bad-direct.zip",
                bad_direct_dir,
                http_client=client,
            )
    assert not (bad_direct_dir / "boot.img").exists()

    # Deflated nested ZIP with invalid partition magic deletes invalid file and raises ValueError
    bad_inner_buf = io.BytesIO()
    with zipfile.ZipFile(bad_inner_buf, "w", compression=zipfile.ZIP_DEFLATED) as inner:
        inner.writestr("boot.img", _make_partition_image("boot", 65536, bad_magic=True))
    bad_outer_buf = io.BytesIO()
    with zipfile.ZipFile(bad_outer_buf, "w", compression=zipfile.ZIP_DEFLATED) as outer:
        outer.writestr("husky/image-husky.zip", bad_inner_buf.getvalue())
    bad_nested_dir = tmp_path / "bad_nested"
    with make_client(bad_outer_buf.getvalue()) as client:
        with pytest.raises(ValueError, match="Invalid partition image magic or size"):
            extract_partitions_via_range(
                "https://dl.google.com/bad-nested.zip",
                bad_nested_dir,
                http_client=client,
            )
    assert not (bad_nested_dir / "boot.img").exists()
