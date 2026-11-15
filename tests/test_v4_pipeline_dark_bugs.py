"""Offline unit and integration tests for v4 pipeline dark bugs & cross-repo contracts."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import socket
import sqlite3
from typing import Any, Sequence
import zipfile

import httpx
import pytest

from fmr_core.crypto import sign_firmware
from fmr_core.d1_client import D1Client, D1QuotaExceededError
from fmr_core.publish_spec import PublishSpecV1
from fmr_extractor.range_extractor import (
    RangeNotSupportedError,
    RemoteZip,
    extract_partitions_via_range,
)
from fmr_ingest_stock.adapters import (
    AdapterDevice,
    AdapterRelease,
    OfficialMetadataHTTP,
    VendorAdapter,
    load_vendor_configs,
)
from fmr_ingest_stock.cdn_rewriter import (
    DNSTransientError,
    FAST_HOST,
    OFFICIAL_OEM_HOSTS,
    validate_official_download_url,
)
from fmr_ingest_stock.download_refresh import PREFIX as REFRESH_PREFIX, fingerprint
from fmr_ingest_stock.pipeline.cli import build_parser, main as cli_main
from fmr_ingest_stock.pipeline.collector import _open_collector_db, run_collector
from fmr_ingest_stock.pipeline.outbox import OutboxSpool
from fmr_ingest_stock.pipeline.publisher import (
    _compute_delivery_sha256,
    _open_ledger_db,
    run_publisher,
)
from fmr_ingest_stock.pipeline.verifier import (
    BASE_DEFERRED_BACKOFF_SECONDS,
    DEAD_UPSTREAM_TTL_SECONDS,
    RangeProbeVerifier,
)

# Deterministic 32-byte Ed25519 seed (64 hex chars) for offline tests
TEST_PRIVATE_KEY_HEX = "11" * 32

FULL_D1_SCHEMA_SQL = """
CREATE TABLE brands (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    bl_unlock_status TEXT NOT NULL,
    sort_order INTEGER NOT NULL
);

CREATE TABLE devices (
    codename TEXT PRIMARY KEY,
    brand_id TEXT NOT NULL,
    marketing_name TEXT NOT NULL,
    model_numbers_json TEXT NOT NULL,
    soc_name TEXT,
    boot_partition_type TEXT NOT NULL,
    has_vbmeta INTEGER NOT NULL DEFAULT 1,
    updated_at INTEGER NOT NULL
);

CREATE TABLE stock_firmwares (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    public_token TEXT NOT NULL UNIQUE,
    device_codename TEXT NOT NULL,
    region_code TEXT NOT NULL,
    model_code TEXT,
    version_str TEXT NOT NULL,
    android_version TEXT,
    package_type TEXT NOT NULL,
    file_size_str TEXT,
    md5_or_sha256 TEXT,
    original_official_url TEXT NOT NULL,
    fast_cdn_url TEXT NOT NULL,
    published_date TEXT,
    ed25519_signature TEXT NOT NULL,
    extract_status TEXT NOT NULL DEFAULT 'pending',
    extract_retries INTEGER NOT NULL DEFAULT 0,
    extract_error TEXT,
    updated_at INTEGER NOT NULL
);

CREATE UNIQUE INDEX idx_fw_identity
ON stock_firmwares (
    device_codename,
    COALESCE(NULLIF(model_code, ''), device_codename),
    region_code,
    version_str,
    package_type
);

CREATE TABLE firmware_metadata (
    public_token TEXT PRIMARY KEY,
    metadata_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE firmware_delivery_specs (
    public_token TEXT PRIMARY KEY,
    delivery_mode TEXT NOT NULL,
    resolver_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);
"""


def _init_d1_sqlite(db_path: Path) -> D1Client:
    conn = sqlite3.connect(str(db_path))
    conn.executescript(FULL_D1_SCHEMA_SQL)
    conn.commit()
    conn.close()
    return D1Client(local_sqlite_path=str(db_path))


# ---------------------------------------------------------------------------
# 1. Cross-repo firmware_delivery_specs.content_hash alignment
# ---------------------------------------------------------------------------


def test_delivery_content_hash_matches_stock_refresh_contract(tmp_path: Path) -> None:
    samsung_source = {
        "schema": 1,
        "vendor": "samsung",
        "version": "S928BXXS4AXK4/S928BOXM4AXK4/S928BXXS4AXK4/S928BXXS4AXK4",
        "model": "SM-S928B",
        "region": "EUX",
        "filename": "SM-S928B_1_20260101_abcd.zip.enc4",
        "path": "/SW_Configuration/Test/",
        "size": 12_345_678_901,
    }
    expected_fus_hash = fingerprint(samsung_source)
    # Verify exact match with stock_refresh.ts sourceHash canonicalization
    js_canonical = json.dumps(
        samsung_source, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    assert expected_fus_hash == hashlib.sha256(js_canonical.encode("utf-8")).hexdigest()

    fus_url = REFRESH_PREFIX + expected_fus_hash
    fus_dev = AdapterDevice(
        brand_id="samsung",
        codename="e3q",
        marketing_name="Galaxy S24 Ultra",
        model_numbers=("SM-S928B",),
    )
    fus_spec_wrapped = AdapterRelease(
        device_codename="e3q",
        model_code="SM-S928B",
        region_code="EUX",
        version_str=samsung_source["version"],
        android_version="15",
        package_type="samsung_encrypted",
        original_official_url=fus_url,
        fast_cdn_url=fus_url,
        file_size_bytes=samsung_source["size"],
        file_size_str="11.50 GB",
        delivery_mode="resolver_fus",
        verification_status="VERIFIED_RESOLVER",
        resolver_meta={"source": samsung_source, "extra_audit": "v4"},
    ).to_publish_spec(fus_dev)
    fus_spec_flat = AdapterRelease(
        device_codename="e3q",
        model_code="SM-S928B",
        region_code="EUX",
        version_str=samsung_source["version"],
        android_version="15",
        package_type="samsung_encrypted",
        original_official_url=fus_url,
        fast_cdn_url=fus_url,
        file_size_bytes=samsung_source["size"],
        file_size_str="11.50 GB",
        delivery_mode="resolver_fus",
        verification_status="VERIFIED_RESOLVER",
        resolver_meta=samsung_source,
    ).to_publish_spec(fus_dev)
    assert _compute_delivery_sha256(fus_spec_wrapped) == expected_fus_hash
    assert _compute_delivery_sha256(fus_spec_flat) == expected_fus_hash

    oplus_source = {
        "schema": 1,
        "vendor": "oneplus",
        "version": "CPH2581_15.0.0.300(EX01)",
        "gate_url": "https://component-ota-sg.allawnos.com/downloadCheck?token=abc123",
        "filename": "CPH2581_15.0.0.300.zip",
    }
    expected_oplus_hash = fingerprint(oplus_source)
    oplus_url = REFRESH_PREFIX + expected_oplus_hash
    oplus_dev = AdapterDevice(
        brand_id="oneplus",
        codename="waffle",
        marketing_name="OnePlus 12",
        model_numbers=("CPH2581",),
    )
    oplus_spec = AdapterRelease(
        device_codename="waffle",
        model_code="CPH2581",
        region_code="GLOBAL",
        version_str=oplus_source["version"],
        android_version="15",
        package_type="recovery_zip",
        original_official_url=oplus_url,
        fast_cdn_url=oplus_url,
        delivery_mode="resolver_oplus",
        verification_status="VERIFIED_RESOLVER",
        resolver_meta={"source": oplus_source},
    ).to_publish_spec(oplus_dev)
    assert _compute_delivery_sha256(oplus_spec) == expected_oplus_hash

    # Mismatched fast_cdn_url hash vs resolver_meta source fingerprint must raise ValueError
    bad_url = REFRESH_PREFIX + ("f" * 64)
    bad_spec = AdapterRelease(
        device_codename="waffle",
        model_code="CPH2581",
        region_code="GLOBAL",
        version_str=oplus_source["version"],
        android_version="15",
        package_type="recovery_zip",
        original_official_url=bad_url,
        fast_cdn_url=bad_url,
        delivery_mode="resolver_oplus",
        verification_status="VERIFIED_RESOLVER",
        resolver_meta={"source": oplus_source},
    ).to_publish_spec(oplus_dev)
    with pytest.raises(ValueError, match="fast_cdn_url refresh hash does not match"):
        _compute_delivery_sha256(bad_spec)

    # Sony R2 bundle suffix extraction
    sony_hash = "ab" * 32
    sony_url = f"https://findmyrom.com/sony/bundles/{sony_hash}"
    sony_dev = AdapterDevice(
        brand_id="sony",
        codename="xq-dq72",
        marketing_name="Xperia 1 V",
        model_numbers=("XQ-DQ72",),
    )
    sony_spec = AdapterRelease(
        device_codename="xq-dq72",
        model_code="XQ-DQ72",
        region_code="SEA",
        version_str="67.1.A.2.208",
        android_version="14",
        package_type="sony_bundle",
        original_official_url=sony_url,
        fast_cdn_url=sony_url,
        delivery_mode="r2_bundle",
        verification_status="VERIFIED_R2_BUNDLE",
        resolver_meta={"bundle_sha256": sony_hash},
    ).to_publish_spec(sony_dev)
    assert _compute_delivery_sha256(sony_spec) == sony_hash

    # End-to-end D1 query verification matching stock_refresh.ts loadRefreshEntry
    state_dir = tmp_path / "state"
    outbox = OutboxSpool(state_dir)
    outbox.enqueue_specs("samsung", [fus_spec_wrapped, oplus_spec])
    db = _init_d1_sqlite(tmp_path / "d1.sqlite")
    try:
        res = run_publisher(
            state_dir=state_dir,
            db=db,
            private_key_hex=TEST_PRIVATE_KEY_HEX,
        )
        assert res["status"] == "ok"
        assert res["published_specs"] == 2

        rows = db.query(
            "SELECT public_token, resolver_json FROM firmware_delivery_specs WHERE content_hash = ?",
            (expected_fus_hash,),
        )
        assert len(rows) == 1
        parsed = json.loads(rows[0]["resolver_json"])
        assert fingerprint(parsed["source"]) == expected_fus_hash
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 2. Crash-safe public_token pre-journaling in publisher.py
# ---------------------------------------------------------------------------


def test_publisher_crash_after_d1_commit_reuses_prejournaled_public_token(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    d1_path = tmp_path / "d1.sqlite"
    base_db = _init_d1_sqlite(d1_path)

    oplus_source = {
        "schema": 1,
        "vendor": "oppo",
        "version": "CPH2609_15.0.0.200(EX01)",
        "gate_url": "https://component-ota-eu.allawnos.com/downloadCheck?token=xyz",
        "filename": "CPH2609_15.0.0.200.zip",
    }
    digest = fingerprint(oplus_source)
    refresh_url = REFRESH_PREFIX + digest

    oppo_dev = AdapterDevice(
        brand_id="oppo",
        codename="ossi",
        marketing_name="OPPO Find X8",
        model_numbers=("CPH2609",),
    )
    spec = AdapterRelease(
        device_codename="ossi",
        model_code="CPH2609",
        region_code="EU",
        version_str=oplus_source["version"],
        android_version="15",
        package_type="recovery_zip",
        original_official_url=refresh_url,
        fast_cdn_url=refresh_url,
        security_patch="2026-02-01",
        os_name="ColorOS",
        os_version="15.0",
        delivery_mode="resolver_oplus",
        verification_status="VERIFIED_RESOLVER",
        resolver_meta={"source": oplus_source},
    ).to_publish_spec(oppo_dev)
    OutboxSpool(state_dir).enqueue_specs("oppo", [spec])

    class CrashAfterD1CommitClient(D1Client):
        def execute_batch(self, statements: Sequence[tuple[str, Sequence[Any]]]) -> list[Any]:
            # D1 commits remotely, then process crashes before ledger hash update!
            super().execute_batch(statements)
            raise RuntimeError("simulated network/process crash after D1 commit")

    crash_db = CrashAfterD1CommitClient(local_sqlite_path=str(d1_path))
    try:
        with pytest.raises(RuntimeError, match="simulated network/process crash"):
            run_publisher(
                state_dir=state_dir,
                db=crash_db,
                private_key_hex=TEST_PRIVATE_KEY_HEX,
            )
    finally:
        crash_db.close()

    # Verify D1 already has the row AND local ledger pre-journaled the exact same public_token
    fw_rows_after_crash = base_db.query("SELECT * FROM stock_firmwares")
    assert len(fw_rows_after_crash) == 1
    d1_token = fw_rows_after_crash[0]["public_token"]

    ledger_conn = _open_ledger_db(state_dir / "publisher_ledger.sqlite")
    try:
        entry = ledger_conn.execute(
            "SELECT * FROM ledger_entries WHERE identity_key = ?",
            (spec.identity_key,),
        ).fetchone()
        assert entry is not None
        assert entry["public_token"] == d1_token
        assert entry["spec_sha256"] is None
    finally:
        ledger_conn.close()

    # Recovery run must reuse d1_token with zero orphaned rows across all 3 tables
    res = run_publisher(
        state_dir=state_dir,
        db=base_db,
        private_key_hex=TEST_PRIVATE_KEY_HEX,
    )
    assert res["status"] == "ok"
    assert res["published_specs"] == 1

    fw_rows = base_db.query("SELECT * FROM stock_firmwares")
    meta_rows = base_db.query("SELECT * FROM firmware_metadata")
    deliv_rows = base_db.query("SELECT * FROM firmware_delivery_specs")
    assert len(fw_rows) == 1
    assert len(meta_rows) == 1
    assert len(deliv_rows) == 1
    assert fw_rows[0]["public_token"] == d1_token
    assert meta_rows[0]["public_token"] == d1_token
    assert deliv_rows[0]["public_token"] == d1_token
    assert deliv_rows[0]["content_hash"] == digest

    expected_sig = sign_firmware(
        public_token=d1_token,
        fast_cdn_url=refresh_url,
        private_key_hex=TEST_PRIVATE_KEY_HEX,
    )
    assert fw_rows[0]["ed25519_signature"] == expected_sig
    base_db.close()


def test_publisher_quota_exceeded_preserves_prejournaled_token_and_migrates_legacy_ledger(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    ledger_path = state_dir / "publisher_ledger.sqlite"
    ledger_path.parent.mkdir(parents=True, exist_ok=True)

    # Seed legacy NOT NULL spec_sha256 schema to verify seamless in-place migration
    raw_conn = sqlite3.connect(str(ledger_path))
    raw_conn.executescript(
        """
        CREATE TABLE ledger_entries (
            identity_key TEXT PRIMARY KEY,
            d1_id INTEGER,
            public_token TEXT NOT NULL,
            spec_sha256 TEXT NOT NULL,
            enrichment_sha256 TEXT,
            delivery_sha256 TEXT,
            updated_at INTEGER NOT NULL
        );
        INSERT INTO ledger_entries VALUES ('["old","old","GL","1.0","recovery_zip"]', 1, 'fw_legacy1', 'hash1', NULL, NULL, 100);
        """
    )
    raw_conn.commit()
    raw_conn.close()

    d1_db = _init_d1_sqlite(tmp_path / "d1.sqlite")
    husky_dev = AdapterDevice(
        brand_id="google",
        codename="husky",
        marketing_name="Pixel 8 Pro",
        model_numbers=("GC3VE",),
    )
    spec = AdapterRelease(
        device_codename="husky",
        model_code="husky",
        region_code="GLOBAL",
        version_str="AP4A.250205.002",
        android_version="15",
        package_type="factory_zip",
        original_official_url="https://dl.google.com/dl/android/aosp/husky-ap4a.zip",
        fast_cdn_url="https://dl.google.com/dl/android/aosp/husky-ap4a.zip",
        file_size_bytes=2_500_000_000,
        file_size_str="2.33 GB",
    ).to_publish_spec(husky_dev)
    OutboxSpool(state_dir).enqueue_specs("google", [spec])

    class QuotaOnBatchClient(D1Client):
        def __init__(self, sqlite_path: Path) -> None:
            super().__init__(local_sqlite_path=str(sqlite_path))
            self.fail = True

        def execute_batch(self, statements: Sequence[tuple[str, Sequence[Any]]]) -> list[Any]:
            if self.fail:
                raise D1QuotaExceededError("daily write quota reached")
            return super().execute_batch(statements)

    d1_sqlite_path = tmp_path / "d1.sqlite"
    d1_db.close()
    proxy = QuotaOnBatchClient(d1_sqlite_path)
    day1 = run_publisher(
        state_dir=state_dir,
        db=proxy,
        private_key_hex=TEST_PRIVATE_KEY_HEX,
        utc_day_fn=lambda: "2026-11-14",
    )
    assert day1["status"] == "paused_d1_quota"

    conn = _open_ledger_db(ledger_path)
    try:
        pre_row = conn.execute(
            "SELECT * FROM ledger_entries WHERE identity_key = ?",
            (spec.identity_key,),
        ).fetchone()
        legacy_row = conn.execute(
            "SELECT * FROM ledger_entries WHERE public_token = 'fw_legacy1'"
        ).fetchone()
        assert pre_row is not None
        assert legacy_row is not None
        reserved_token = pre_row["public_token"]
        assert pre_row["spec_sha256"] is None
    finally:
        conn.close()

    proxy.fail = False
    day2 = run_publisher(
        state_dir=state_dir,
        db=proxy,
        private_key_hex=TEST_PRIVATE_KEY_HEX,
        utc_day_fn=lambda: "2026-11-15",
    )
    assert day2["status"] == "ok"
    assert day2["published_specs"] == 1
    fw_rows = proxy.query("SELECT public_token FROM stock_firmwares")
    assert len(fw_rows) == 1
    assert fw_rows[0]["public_token"] == reserved_token
    proxy.close()


# ---------------------------------------------------------------------------
# 3. Incremental collector, separate HTTP cache SQLite conn, & max_devices
# ---------------------------------------------------------------------------


def test_vendors_yaml_and_cli_max_devices() -> None:
    configs = load_vendor_configs()
    expected_active = {
        "google": 25,
        "samsung": 15,
        "xiaomi": 40,
        "oneplus": 25,
        "oppo": 25,
        "realme": 25,
        "asus": 60,
        "vivo": 60,
    }
    assert configs["google"].verify_workers == 1
    for vid, expected_max in expected_active.items():
        assert configs[vid].enabled is True
        assert configs[vid].max_devices == expected_max

    for disabled_vid in ("rog", "motorola", "nothing", "tecno", "infinix", "itel", "sony", "huawei", "honor"):
        assert configs[disabled_vid].enabled is False

    parser = build_parser()
    args = parser.parse_args(["collect", "--vendor", "samsung", "--max-devices", "7", "--no-dns-check"])
    assert args.max_devices == 7


def test_collector_incremental_per_device_rotation_and_separate_http_cache_conn(
    tmp_path: Path,
) -> None:
    observed_cache_conns: list[sqlite3.Connection] = []

    class IncrementalAdapter(VendorAdapter):
        vendor_id = "google"

        def discover_devices(self, http: OfficialMetadataHTTP) -> Sequence[AdapterDevice]:
            if http._cache_conn is not None:
                observed_cache_conns.append(http._cache_conn)
            return [
                AdapterDevice(
                    brand_id="google",
                    codename=f"pixel_{i}",
                    marketing_name=f"Pixel {i}",
                    model_numbers=(f"GPX{i}",),
                )
                for i in range(5)
            ]

        def fetch_releases(
            self, device: AdapterDevice, http: OfficialMetadataHTTP
        ) -> Sequence[AdapterRelease]:
            # Trigger an HTTP metadata cache write via OfficialMetadataHTTP during device crawl
            http.get(f"https://developers.google.com/android/ota?dev={device.codename}")
            url = f"https://dl.google.com/dl/android/aosp/{device.codename}-ota.zip"
            return [
                AdapterRelease(
                    device_codename=device.codename,
                    model_code=device.codename,
                    region_code="GLOBAL",
                    version_str=f"AP4A.{device.codename}",
                    android_version="15",
                    package_type="recovery_zip",
                    original_official_url=url,
                    fast_cdn_url=url,
                )
            ]

    meta_client = httpx.Client(
        transport=httpx.MockTransport(
            lambda req: httpx.Response(200, text="<html>official ota</html>")
        )
    )
    zip_head = b"PK\x03\x04" + (b"\x00" * 60)
    range_client = httpx.Client(
        transport=httpx.MockTransport(
            lambda req: httpx.Response(
                206,
                headers={
                    "Content-Range": f"bytes 0-63/{120 * 1024 * 1024}",
                    "Content-Type": "application/zip",
                },
                content=zip_head,
            )
        )
    )

    state_dir = tmp_path / "state"
    now = [1000]

    def clock() -> int:
        now[0] += 1
        return now[0]

    try:
        # Run 1: max_devices=2 checks pixel_0 and pixel_1 only
        r1 = run_collector(
            "google",
            state_dir=state_dir,
            adapter=IncrementalAdapter(),
            metadata_client=meta_client,
            range_client=range_client,
            max_devices=2,
            resolve_dns=False,
            now_fn=clock,
        )
        assert r1["devices_discovered"] == 5
        assert r1["devices_checked"] == 2
        assert r1["probed_verified"] == 2
        assert r1["spooled_specs"] == 2

        vconn = _open_collector_db(state_dir / "collectors" / "google.sqlite")
        try:
            assert observed_cache_conns
            assert observed_cache_conns[0] is not vconn
            rows = {
                r["codename"]: int(r["last_checked_at"])
                for r in vconn.execute(
                    "SELECT codename, last_checked_at FROM collector_devices"
                ).fetchall()
            }
            assert rows["pixel_0"] > 0
            assert rows["pixel_1"] > 0
            assert rows["pixel_2"] == 0
            assert rows["pixel_3"] == 0
            assert rows["pixel_4"] == 0
        finally:
            vconn.close()

        # Run 2: max_devices=2 rotates to pixel_2 and pixel_3
        r2 = run_collector(
            "google",
            state_dir=state_dir,
            adapter=IncrementalAdapter(),
            metadata_client=meta_client,
            range_client=range_client,
            max_devices=2,
            resolve_dns=False,
            now_fn=clock,
        )
        assert r2["devices_checked"] == 2
        assert r2["probed_verified"] == 2
        assert r2["spooled_specs"] == 2

        # Run 3: max_devices=2 checks pixel_4 and re-checks pixel_0 (cached probe + unchanged spool)
        r3 = run_collector(
            "google",
            state_dir=state_dir,
            adapter=IncrementalAdapter(),
            metadata_client=meta_client,
            range_client=range_client,
            max_devices=2,
            resolve_dns=False,
            now_fn=clock,
        )
        assert r3["devices_checked"] == 2
        assert r3["probed_verified"] == 1
        assert r3["skipped_cached_probe"] == 1
        assert r3["unchanged_not_spooled"] == 1
        assert r3["spooled_specs"] == 1
    finally:
        meta_client.close()
        range_client.close()


def test_collector_per_device_http_error_isolation_and_discover_still_raises(
    tmp_path: Path,
) -> None:
    fail_dev_1 = {"enabled": True}

    class FlakyDeviceAdapter(VendorAdapter):
        vendor_id = "samsung"

        def discover_devices(self, http: OfficialMetadataHTTP) -> Sequence[AdapterDevice]:
            return [
                AdapterDevice(
                    brand_id="samsung",
                    codename=f"dev_{i}",
                    marketing_name=f"Galaxy {i}",
                    model_numbers=(f"SM-S90{i}B",),
                )
                for i in range(3)
            ]

        def fetch_releases(
            self, device: AdapterDevice, http: OfficialMetadataHTTP
        ) -> Sequence[AdapterRelease]:
            if device.codename == "dev_1" and fail_dev_1["enabled"]:
                req = httpx.Request(
                    "POST", "https://neofussvr.sslcs.cdngc.net/NF_SmartDownloadBinaryInform.do"
                )
                resp = httpx.Response(502, request=req, text="502 Bad Gateway")
                raise httpx.HTTPStatusError("502 Bad Gateway", request=req, response=resp)

            fus_source = {
                "schema": 1,
                "vendor": "samsung",
                "model": device.model_numbers[0],
                "region": "EUX",
                "version": f"S90XBXXS1AXK1/S90XBOXM1AXK1/S90XBXXS1AXK1/S90XBXXS1AXK1",
                "filename": f"{device.model_numbers[0]}_1_20261101120000_eux.zip.enc4",
                "path": f"/SmartSwitch/{device.model_numbers[0]}/",
                "size": 7_000_000_000,
            }
            url = REFRESH_PREFIX + fingerprint(fus_source)
            return [
                AdapterRelease(
                    device_codename=device.codename,
                    model_code=device.model_numbers[0],
                    region_code="EUX",
                    version_str=fus_source["version"],
                    android_version="15",
                    package_type="samsung_encrypted",
                    original_official_url=url,
                    fast_cdn_url=url,
                    file_size_bytes=fus_source["size"],
                    file_size_str="6.52 GB",
                    delivery_mode="resolver_fus",
                    verification_status="VERIFIED_RESOLVER",
                    resolver_meta={"source": fus_source},
                )
            ]

    class BrokenDiscoverAdapter(VendorAdapter):
        vendor_id = "samsung"

        def discover_devices(self, http: OfficialMetadataHTTP) -> Sequence[AdapterDevice]:
            raise httpx.ConnectError("upstream discovery down")

        def fetch_releases(
            self, device: AdapterDevice, http: OfficialMetadataHTTP
        ) -> Sequence[AdapterRelease]:
            return []

    meta_client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200)))
    range_client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200)))
    state_dir = tmp_path / "state"
    now = [2000]

    def clock() -> int:
        now[0] += 1
        return now[0]

    try:
        # 1. Run 1: dev_1 raises 502 HTTPStatusError; dev_0 and dev_2 succeed, dev_1 stays due (last_checked_at == 0)
        r1 = run_collector(
            "samsung",
            state_dir=state_dir,
            adapter=FlakyDeviceAdapter(),
            metadata_client=meta_client,
            range_client=range_client,
            resolve_dns=False,
            now_fn=clock,
        )
        assert r1["status"] == "ok"
        assert r1["devices_discovered"] == 3
        assert r1["devices_checked"] == 3
        assert r1["device_fetch_errors"] == 1
        assert r1["spooled_specs"] == 2

        vconn = _open_collector_db(state_dir / "collectors" / "samsung.sqlite")
        try:
            checked_map = {
                r["codename"]: int(r["last_checked_at"])
                for r in vconn.execute(
                    "SELECT codename, last_checked_at FROM collector_devices"
                ).fetchall()
            }
            assert checked_map["dev_0"] > 0
            assert checked_map["dev_1"] == 0
            assert checked_map["dev_2"] > 0
        finally:
            vconn.close()

        spooled_codenames = {
            PublishSpecV1.from_dict(json.loads(line)).device.codename
            for path in OutboxSpool(state_dir).list_pending()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        }
        assert spooled_codenames == {"dev_0", "dev_2"}

        # 2. Run 2 with max_devices=1 and dev_1 recovered: dev_1 (last_checked_at==0) is prioritized first!
        fail_dev_1["enabled"] = False
        r2 = run_collector(
            "samsung",
            state_dir=state_dir,
            adapter=FlakyDeviceAdapter(),
            metadata_client=meta_client,
            range_client=range_client,
            max_devices=1,
            resolve_dns=False,
            now_fn=clock,
        )
        assert r2["status"] == "ok"
        assert r2["devices_checked"] == 1
        assert r2["device_fetch_errors"] == 0
        assert r2["spooled_specs"] == 1

        vconn = _open_collector_db(state_dir / "collectors" / "samsung.sqlite")
        try:
            dev1_ts = int(
                vconn.execute(
                    "SELECT last_checked_at FROM collector_devices WHERE codename = 'dev_1'"
                ).fetchone()["last_checked_at"]
            )
            assert dev1_ts > checked_map["dev_2"]
        finally:
            vconn.close()

        # 3. discover_devices HTTPError still raises normally instead of being swallowed
        with pytest.raises(httpx.ConnectError, match="upstream discovery down"):
            run_collector(
                "samsung",
                state_dir=state_dir,
                adapter=BrokenDiscoverAdapter(),
                metadata_client=meta_client,
                range_client=range_client,
                resolve_dns=False,
                now_fn=clock,
            )
    finally:
        meta_client.close()
        range_client.close()


def test_http_cache_commit_does_not_commit_collector_conn_transaction(tmp_path: Path) -> None:
    """Prove separate SQLite connections prevent OfficialMetadataHTTP from committing conn."""
    db_path = tmp_path / "collectors" / "test.sqlite"
    conn = _open_collector_db(db_path)
    http_cache_conn = sqlite3.connect(str(db_path), timeout=0.05)
    http_cache_conn.row_factory = sqlite3.Row
    http_cache_conn.execute("PRAGMA journal_mode = WAL;")
    http_cache_conn.execute("PRAGMA busy_timeout = 50;")

    meta_client = httpx.Client(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, text="ok"))
    )
    try:
        # 1. When conn is idle, http_cache_conn writes & commits http_cache cleanly in WAL mode
        with OfficialMetadataHTTP(
            client=meta_client,
            cache_conn=http_cache_conn,
            sleep_fn=lambda _: None,
        ) as http:
            resp = http.get("https://developers.google.com/android/ota")
            assert resp.text == "ok"

            # 2. Start an uncommitted write on conn; a cache hit on http_cache_conn reads fine
            # and NEVER commits conn's open transaction, and a new write on http_cache_conn
            # cannot silently commit conn's uncommitted row either!
            conn.execute(
                """
                INSERT INTO collector_devices (
                    codename, brand_id, marketing_name, model_numbers_json,
                    boot_partition_type, device_sha256, updated_at, last_checked_at
                ) VALUES ('uncommitted_dev', 'google', 'Test', '[]', 'init_boot', 'abc', 1, 999)
                """
            )
            assert conn.in_transaction is True

            with pytest.raises(sqlite3.OperationalError):
                http.get("https://developers.google.com/android/ota?miss=1")

            # conn's transaction was NEVER committed; rollback discards uncommitted_dev!
            assert conn.in_transaction is True
            conn.rollback()
            row = conn.execute(
                "SELECT * FROM collector_devices WHERE codename = 'uncommitted_dev'"
            ).fetchone()
            assert row is None
    finally:
        meta_client.close()
        http_cache_conn.close()
        conn.close()


# ---------------------------------------------------------------------------
# 4. Unified CDN allowlist & DNSTransientError + 429 Retry-After
# ---------------------------------------------------------------------------


def test_extraction_matrix_byte_identical_and_cdn_allowlist_synced() -> None:
    runner_dir = Path(__file__).resolve().parents[1]
    runner_matrix = runner_dir / "fmr_core" / "contracts" / "extraction_matrix.v1.json"
    web_matrix = runner_dir.parent / "findmyrom-web" / "src" / "contracts" / "extraction_matrix.v1.json"

    runner_bytes = runner_matrix.read_bytes()
    if web_matrix.exists():
        web_bytes = web_matrix.read_bytes()
        assert runner_bytes == web_bytes, "extraction_matrix.v1.json copies must be byte-for-byte identical"

    matrix_data = json.loads(runner_bytes.decode("utf-8"))
    cdn_hosts = matrix_data["official_cdn_hosts"]

    from fmr_ingest_stock.cdn_rewriter import _FALLBACK_OFFICIAL_CDN_HOSTS

    for brand, hosts in cdn_hosts.items():
        assert hosts == sorted(hosts), f"official_cdn_hosts[{brand!r}] must remain sorted"
        assert OFFICIAL_OEM_HOSTS[brand] == tuple(hosts)
        assert _FALLBACK_OFFICIAL_CDN_HOSTS[brand] == tuple(hosts)
    assert set(_FALLBACK_OFFICIAL_CDN_HOSTS) == set(cdn_hosts)

    # Brand aliases match their parent OEMs
    assert OFFICIAL_OEM_HOSTS["pixel"] == OFFICIAL_OEM_HOSTS["google"]
    assert OFFICIAL_OEM_HOSTS["redmi"] == OFFICIAL_OEM_HOSTS["xiaomi"]
    assert OFFICIAL_OEM_HOSTS["poco"] == OFFICIAL_OEM_HOSTS["xiaomi"]
    assert OFFICIAL_OEM_HOSTS["iqoo"] == OFFICIAL_OEM_HOSTS["vivo"]

    # Newly added official CDN hosts validate cleanly
    assert validate_official_download_url(
        "oneplus", "https://download.oneplusmobile.com/ota/pkg.zip", resolve_dns=False
    )
    assert validate_official_download_url(
        "oppo", "https://gauss-componentotacostmanual-eu.allawnofs.com/ota.zip", resolve_dns=False
    )
    assert validate_official_download_url(
        "oppo", "https://ota.oplusmobile.com/pkg.zip", resolve_dns=False
    )
    assert validate_official_download_url(
        "realme", "https://download.realme.net/ota/pkg.zip", resolve_dns=False
    )
    assert validate_official_download_url(
        "samsung", "https://fota-s3-dn.ospserver.net/firmware/pkg.zip", resolve_dns=False
    )
    assert validate_official_download_url(
        "samsung", "https://d2s.sslcs.cdngc.net/firmware/pkg.zip", resolve_dns=False
    )


def test_dns_transient_defers_while_ssrf_rebinding_dead_upstreams_and_429_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rel = AdapterRelease(
        device_codename="husky",
        model_code="husky",
        region_code="GLOBAL",
        version_str="AP4A.250205.002",
        android_version="15",
        package_type="recovery_zip",
        original_official_url="https://dl.google.com/dl/android/aosp/husky.zip",
        fast_cdn_url="https://dl.google.com/dl/android/aosp/husky.zip",
    )

    # 1. Transient DNS failure on initial URL -> DEFERRED_VERIFY (15 min, NOT rejected_unofficial)
    def raise_gaierror(host: str, port: int, type: int = 0) -> list[Any]:
        raise socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution")

    monkeypatch.setattr(socket, "getaddrinfo", raise_gaierror)
    with pytest.raises(DNSTransientError):
        validate_official_download_url(
            "google", "https://dl.google.com/dl/android/aosp/husky.zip", resolve_dns=True
        )

    client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200)))
    try:
        with RangeProbeVerifier(client=client, resolve_dns=True) as verifier:
            outcome = verifier.verify_one("google", rel, failure_count=0, raise_on_unofficial=True)
            assert outcome.status == "DEFERRED_VERIFY"
            assert outcome.rejected_unofficial is False
            assert outcome.retry_after_seconds == BASE_DEFERRED_BACKOFF_SECONDS

        # 2. Transient DNS failure on redirect hop -> DEFERRED_VERIFY
        def dns_ok_first_then_fail(host: str, port: int, type: int = 0) -> list[Any]:
            if host == "dl.google.com":
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("142.250.190.78", 443))]
            raise socket.gaierror(socket.EAI_AGAIN, "Temporary failure on redirect mirror")

        monkeypatch.setattr(socket, "getaddrinfo", dns_ok_first_then_fail)
        redirect_client = httpx.Client(
            transport=httpx.MockTransport(
                lambda req: httpx.Response(
                    302,
                    headers={"Location": "https://dl.googleusercontent.com/mirror/husky.zip"},
                )
            )
        )
        try:
            with RangeProbeVerifier(client=redirect_client, resolve_dns=True) as verifier:
                redir_outcome = verifier.verify_one("google", rel, failure_count=0)
                assert redir_outcome.status == "DEFERRED_VERIFY"
                assert redir_outcome.rejected_unofficial is False
                assert redir_outcome.retry_after_seconds == BASE_DEFERRED_BACKOFF_SECONDS
        finally:
            redirect_client.close()

        # 3. DNS rebinding to private/loopback IP MUST still be DEAD_UPSTREAM + rejected_unofficial
        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            lambda host, port, type=0: [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))
            ],
        )
        with RangeProbeVerifier(client=client, resolve_dns=True) as verifier:
            rebind_outcome = verifier.verify_one("google", rel)
            assert rebind_outcome.status == "DEAD_UPSTREAM"
            assert rebind_outcome.rejected_unofficial is True
            assert rebind_outcome.retry_after_seconds == DEAD_UPSTREAM_TTL_SECONDS

        # 4. HTTP 429 Retry-After clamping [60, 86400] and max(backoff, retry_after)
        for header_val, failure_cnt, expected_wait in [
            ("3600", 0, 3600),
            ("999999", 0, 86400),
            ("10", 0, 900),  # clamped to 60, max(900, 60) == 900
        ]:
            rate_client = httpx.Client(
                transport=httpx.MockTransport(
                    lambda req, hv=header_val: httpx.Response(429, headers={"Retry-After": hv})
                )
            )
            try:
                with RangeProbeVerifier(client=rate_client, resolve_dns=False) as verifier:
                    r429 = verifier.verify_one("google", rel, failure_count=failure_cnt)
                    assert r429.status == "DEFERRED_VERIFY"
                    assert r429.retry_after_seconds == expected_wait
            finally:
                rate_client.close()
    finally:
        client.close()


# ---------------------------------------------------------------------------
# 5. RemoteZip CDN rewrite & safe HTTPS redirect following
# ---------------------------------------------------------------------------


def _make_sample_zip_bytes() -> tuple[bytes, bytes]:
    buf = io.BytesIO()
    boot = b"ANDROID!" + (b"\x00" * (65536 - 8))
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr("boot.img", boot)
    return buf.getvalue(), boot


def test_remote_zip_rewrites_miui_and_follows_safe_https_redirects(tmp_path: Path) -> None:
    blob, boot_bytes = _make_sample_zip_bytes()
    seen_requests: list[tuple[str, str | None]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen_requests.append((str(req.url), req.headers.get("If-Match")))
        if req.url.host == FAST_HOST and req.url.path == "/V1/ota.zip":
            return httpx.Response(
                302,
                headers={"Location": "https://dl.google.com/hop1/ota.zip"},
            )
        if req.url.host == "dl.google.com" and req.url.path == "/hop1/ota.zip":
            return httpx.Response(
                307,
                headers={"Location": "https://dl.googleusercontent.com/final/ota.zip"},
            )
        if req.url.host == "dl.googleusercontent.com" and req.url.path == "/final/ota.zip":
            start_s, end_s = req.headers["Range"].removeprefix("bytes=").split("-")
            start, end = int(start_s), int(end_s)
            return httpx.Response(
                206,
                headers={
                    "Content-Range": f"bytes {start}-{end}/{len(blob)}",
                    "ETag": '"etag-strong-v1"',
                },
                stream=httpx.ByteStream(blob[start : end + 1]),
            )
        raise AssertionError(f"Unexpected URL: {req.url}")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        images = extract_partitions_via_range(
            "https://bigota.d.miui.com/V1/ota.zip",
            tmp_path,
            target_partitions=("boot",),
            http_client=client,
        )
        assert images["boot"].read_bytes() == boot_bytes

    # First probe started at FAST_HOST (proving rewrite_to_fast_cdn return value captured!),
    # followed 2 redirects to dl.googleusercontent.com, and ALL subsequent Range reads went
    # directly to dl.googleusercontent.com with If-Match: "etag-strong-v1"!
    assert seen_requests[0] == (f"https://{FAST_HOST}/V1/ota.zip", None)
    assert seen_requests[1] == ("https://dl.google.com/hop1/ota.zip", None)
    assert seen_requests[2] == ("https://dl.googleusercontent.com/final/ota.zip", None)
    assert len(seen_requests) >= 4
    for url_str, if_match in seen_requests[3:]:
        assert url_str == "https://dl.googleusercontent.com/final/ota.zip"
        assert if_match == '"etag-strong-v1"'


@pytest.mark.parametrize(
    "bad_location",
    [
        "http://dl.google.com/downgrade.zip",
        "http://d.miui.com/legacy-downgrade.zip",
        "https://localhost/ota.zip",
        "https://127.0.0.1/ota.zip",
        "https://169.254.169.254/latest/meta-data/",
        "https://user:pass@dl.google.com/ota.zip",
        "https://dl.google.com:8443/ota.zip",
    ],
)
def test_remote_zip_rejects_unsafe_redirect_targets(bad_location: str) -> None:
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda req: httpx.Response(302, headers={"Location": bad_location})
        )
    ) as client:
        with pytest.raises(ValueError):
            RemoteZip("https://dl.google.com/ota.zip", client, max_bytes=1024 * 1024)


def test_remote_zip_enforces_max_3_redirects() -> None:
    def loop_handler(req: httpx.Request) -> httpx.Response:
        hop = int(req.url.params.get("hop", "0"))
        return httpx.Response(
            302,
            headers={"Location": f"https://dl.google.com/ota.zip?hop={hop + 1}"},
        )

    with httpx.Client(transport=httpx.MockTransport(loop_handler)) as client:
        with pytest.raises(RangeNotSupportedError, match="Too many HTTP redirects"):
            RemoteZip("https://dl.google.com/ota.zip", client, max_bytes=1024 * 1024)


# ---------------------------------------------------------------------------
# 6. Publisher UTC Midnight Rollover Brake (08:00 CST / 00:00 UTC)
# ---------------------------------------------------------------------------


def _make_google_specs(count: int) -> list[PublishSpecV1]:
    dev = AdapterDevice(
        brand_id="google",
        codename="husky",
        marketing_name="Pixel 8 Pro",
        model_numbers=("GC3VE",),
    )
    specs: list[PublishSpecV1] = []
    for i in range(count):
        url = f"https://dl.google.com/dl/android/aosp/husky-v{i}.zip"
        specs.append(
            AdapterRelease(
                device_codename="husky",
                model_code="husky",
                region_code="GLOBAL",
                version_str=f"AP4A.250205.{i:03d}",
                android_version="15",
                package_type="factory_zip",
                original_official_url=url,
                fast_cdn_url=url,
                file_size_bytes=2_500_000_000,
                file_size_str="2.33 GB",
            ).to_publish_spec(dev)
        )
    return specs


def test_publisher_utc_day_rollover_pauses_cleanly_without_blocking_new_day(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    d1_path = tmp_path / "d1.sqlite"
    base_db = _init_d1_sqlite(d1_path)
    outbox = OutboxSpool(state_dir)
    outbox.enqueue_specs("google", _make_google_specs(3), batch_size=10)

    # First batch commits on 2026-11-15; when execute_batch finishes, UTC day flips to 2026-11-16
    current_day = ["2026-11-15"]

    class RolloverOnBatchClient(D1Client):
        def execute_batch(self, statements: Sequence[tuple[str, Sequence[Any]]]) -> list[Any]:
            res = super().execute_batch(statements)
            current_day[0] = "2026-11-16"
            return res

    base_db.close()
    roll_db = RolloverOnBatchClient(local_sqlite_path=str(d1_path))
    try:
        res = run_publisher(
            state_dir=state_dir,
            db=roll_db,
            private_key_hex=TEST_PRIVATE_KEY_HEX,
            batch_size=1,
            utc_day_fn=lambda: current_day[0],
            now_fn=lambda: 1763251000,  # 23:56:40 UTC
        )
        assert res["status"] == "paused_utc_rollover"
        assert res["published_specs"] == 1
        assert res["batches_committed"] == 1
        assert res["pending_files_remaining"] == 1

        # Uncommitted specs (2 of 3) were rewritten into outbox/pending/
        pending = outbox.list_pending()
        assert len(pending) == 1
        remaining_specs = outbox.read_specs(pending[0])
        assert [s.release.version_str for s in remaining_specs] == [
            "AP4A.250205.001",
            "AP4A.250205.002",
        ]

        # Must NOT set blocked_until_utc_day because the new UTC day has fresh quota!
        ledger_conn = _open_ledger_db(state_dir / "publisher_ledger.sqlite")
        try:
            blocked_row = ledger_conn.execute(
                "SELECT value FROM publisher_state WHERE key = 'blocked_until_utc_day'"
            ).fetchone()
            assert blocked_row is None
        finally:
            ledger_conn.close()

        # Subsequent run on 2026-11-16 finishes the remaining 2 specs immediately
        res_next = run_publisher(
            state_dir=state_dir,
            db=roll_db,
            private_key_hex=TEST_PRIVATE_KEY_HEX,
            batch_size=1,
            utc_day_fn=lambda: "2026-11-16",
            now_fn=lambda: 1763251300,
        )
        assert res_next["status"] == "ok"
        assert res_next["published_specs"] == 2
        assert res_next["pending_files_remaining"] == 0
        assert len(roll_db.query("SELECT id FROM stock_firmwares")) == 3
    finally:
        roll_db.close()


def test_publisher_stops_before_utc_midnight_window(tmp_path: Path) -> None:
    import datetime as dt

    state_dir = tmp_path / "state"
    d1_path = tmp_path / "d1.sqlite"
    base_db = _init_d1_sqlite(d1_path)
    outbox = OutboxSpool(state_dir)
    outbox.enqueue_specs("google", _make_google_specs(3), batch_size=10)

    # Start at 23:58:50 UTC (70s before midnight -> allowed for chunk 1),
    # then after chunk 1 commits advance clock to 23:59:05 UTC (55s before midnight <= 60s brake!).
    t_before = int(dt.datetime(2026, 11, 15, 23, 58, 50, tzinfo=dt.timezone.utc).timestamp())
    t_brake = int(dt.datetime(2026, 11, 15, 23, 59, 5, tzinfo=dt.timezone.utc).timestamp())
    clock_ts = [t_before]

    class AdvanceClockClient(D1Client):
        def execute_batch(self, statements: Sequence[tuple[str, Sequence[Any]]]) -> list[Any]:
            res = super().execute_batch(statements)
            clock_ts[0] = t_brake
            return res

    base_db.close()
    db = AdvanceClockClient(local_sqlite_path=str(d1_path))
    try:
        res = run_publisher(
            state_dir=state_dir,
            db=db,
            private_key_hex=TEST_PRIVATE_KEY_HEX,
            batch_size=1,
            utc_day_fn=lambda: "2026-11-15",
            now_fn=lambda: clock_ts[0],
            stop_before_utc_midnight_seconds=60,
        )
        assert res["status"] == "paused_utc_rollover"
        assert res["published_specs"] == 1
        assert res["pending_files_remaining"] == 1

        pending = outbox.list_pending()
        assert len(pending) == 1
        assert len(outbox.read_specs(pending[0])) == 2

        ledger_conn = _open_ledger_db(state_dir / "publisher_ledger.sqlite")
        try:
            blocked_row = ledger_conn.execute(
                "SELECT value FROM publisher_state WHERE key = 'blocked_until_utc_day'"
            ).fetchone()
            assert blocked_row is None
        finally:
            ledger_conn.close()
    finally:
        db.close()
