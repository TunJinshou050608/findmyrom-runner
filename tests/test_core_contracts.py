"""Unit and contract tests for fmr_core V1 extraction matrix, PublishSpecV1, D1 quota errors, and schema v7."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from unittest.mock import MagicMock, patch

import pytest
import requests

from fmr_core.d1_client import (
    D1BatchError,
    D1Client,
    D1QuotaExceededError,
    is_d1_quota_error,
)
from fmr_core.extraction_policy import (
    EXTRACTION_BRANDS,
    EXTRACTION_PACKAGES,
    extraction_eligible,
    load_extraction_matrix,
    official_cdn_hosts,
    package_priority,
    partition_limits,
)
from fmr_core.publish_spec import (
    DeviceSpec,
    EnrichmentSpec,
    PublishSpecV1,
    ReleaseSpec,
    canonical_version_str,
)
from fmr_core.schema_migrations import CURRENT, ensure_schema, main as migrations_main
from fmr_ingest_stock.device_sync import ensure_base_brands_and_anchor

RUNNER_ROOT = Path(__file__).parents[1]


# ---------------------------------------------------------------------------
# 1. Extraction Matrix V1 & extraction_policy
# ---------------------------------------------------------------------------


def test_extraction_matrix_v1_contract_and_helpers():
    contract_path = RUNNER_ROOT / "fmr_core" / "contracts" / "extraction_matrix.v1.json"
    raw_text = contract_path.read_text(encoding="utf-8")
    assert raw_text.endswith("\n")
    parsed = json.loads(raw_text)
    assert raw_text == json.dumps(parsed, indent=2) + "\n"

    web_contract_path = RUNNER_ROOT.parent / "findmyrom-web" / "src" / "contracts" / "extraction_matrix.v1.json"
    if web_contract_path.exists():
        runner_sha = hashlib.sha256(contract_path.read_bytes()).hexdigest()
        web_sha = hashlib.sha256(web_contract_path.read_bytes()).hexdigest()
        assert web_sha == runner_sha, (
            "findmyrom-web/src/contracts/extraction_matrix.v1.json must match "
            "findmyrom-runner/fmr_core/contracts/extraction_matrix.v1.json byte-for-byte!"
        )

    load_extraction_matrix.cache_clear()
    m1 = load_extraction_matrix()
    m2 = load_extraction_matrix()
    assert m1 is m2
    assert load_extraction_matrix.cache_info().hits >= 1
    assert m1["schema_version"] == 1

    assert isinstance(EXTRACTION_BRANDS, frozenset)
    assert isinstance(EXTRACTION_PACKAGES, frozenset)
    assert EXTRACTION_BRANDS == frozenset(
        {"asus", "google", "infinix", "itel", "motorola", "nothing", "oneplus", "oppo", "realme", "rog", "tecno", "vivo", "xiaomi"}
    )
    assert EXTRACTION_PACKAGES == frozenset({"factory_zip", "fastboot_zip", "recovery_zip"})

    limits = partition_limits()
    assert limits["init_boot"] == (65536, 67108864, b"ANDROID!")
    assert limits["boot"] == (65536, 201326592, b"ANDROID!")
    assert limits["vendor_boot"] == (65536, 201326592, b"VNDRBOOT")
    assert limits["recovery"] == (65536, 268435456, b"ANDROID!")
    assert limits["vbmeta"] == (4096, 1048576, b"AVB0")

    assert official_cdn_hosts("Google") == ("dl.google.com", "dl.googleusercontent.com")
    assert "d.miui.com" in official_cdn_hosts("xiaomi")
    assert official_cdn_hosts("unknown_vendor") == ()

    prio = package_priority()
    assert prio == (
        "fastboot_tgz",
        "fastboot_zip",
        "factory_zip",
        "recovery_zip",
        "recovery_ozip",
        "samsung_zip",
        "samsung_encrypted",
        "sony_bundle",
        "incremental_zip",
    )


def test_extraction_eligible_preserves_exact_semantics():
    assert extraction_eligible("oneplus", "recovery_zip", "https://ota.oneplus.net/rom/build.zip") is True
    assert extraction_eligible("tecno", "recovery_zip", "https://android.googleapis.com/packages/ota-api/package/aabbcc112233.zip") is True
    assert extraction_eligible("google", "factory_zip", "https://dl.google.com/dl/android/aosp/husky.ZIP") is True
    assert extraction_eligible("samsung", "samsung_zip", "https://samsungmobile.com/fw.zip") is False
    assert extraction_eligible("oneplus", "fastboot_tgz", "https://ota.oneplus.net/rom/build.tgz") is False
    assert extraction_eligible("oneplus", "recovery_zip", "http://ota.oneplus.net/rom/build.zip") is False
    assert extraction_eligible("oneplus", "recovery_zip", "https://findmyrom.com/rom/build.zip") is False
    assert extraction_eligible("oneplus", "recovery_zip", "https://ota.oneplus.net/rom/build.zip?token=1") is False
    assert extraction_eligible("oneplus", "recovery_zip", "https://ota.oneplus.net/rom/build.zip#part") is False
    assert extraction_eligible("oneplus", "recovery_zip", "https://user:pass@ota.oneplus.net/rom/build.zip") is False


# ---------------------------------------------------------------------------
# 2. PublishSpecV1 validation, serialization, and deterministic hashing
# ---------------------------------------------------------------------------


def _sample_device(**overrides) -> DeviceSpec:
    base = {
        "codename": "husky",
        "brand_id": "google",
        "marketing_name": "Pixel 8 Pro",
        "model_numbers": ("GC3VE", "G1MNW"),
        "soc_name": "Google Tensor G3",
        "boot_partition_type": "init_boot",
        "has_vbmeta": 1,
    }
    base.update(overrides)
    return DeviceSpec(**base)


def _sample_release(**overrides) -> ReleaseSpec:
    base = {
        "device_codename": "husky",
        "model_code": "husky",
        "region_code": "Global",
        "version_str": "AP2A.240805.005",
        "android_version": "14",
        "package_type": "factory_zip",
        "original_official_url": "https://dl.google.com/dl/android/aosp/husky-ap2a.240805.005-factory.zip",
        "fast_cdn_url": "https://dl.google.com/dl/android/aosp/husky-ap2a.240805.005-factory.zip",
        "file_size_bytes": 2684354560,
        "file_size_str": "2.50 GB",
        "md5_or_sha256": "a" * 64,
        "published_date": "2024-08-05",
        "delivery_mode": "direct_cdn",
        "verification_status": "VERIFIED_IMMUTABLE",
        "resolver_meta": None,
    }
    base.update(overrides)
    return ReleaseSpec(**base)


def _sample_enrichment(**overrides) -> EnrichmentSpec:
    base = {
        "display_version": "AP2A.240805.005",
        "raw_version": "14.0.0 (AP2A.240805.005, Aug 2024)",
        "os_name": "Android",
        "os_version": "14",
        "security_patch": "2024-08-05",
        "original_filename": "husky-ap2a.240805.005-factory.zip",
        "vendor_fields": {"channel": "stable"},
        "sources": {"official_page": "https://developers.google.com/android/images"},
        "availability": "available",
        "unavailable_reason": None,
    }
    base.update(overrides)
    return EnrichmentSpec(**base)


def test_canonical_version_str_normalization_and_truncation():
    assert canonical_version_str("OS1.0.5.0.UMCCNXM") == "OS1.0.5.0.UMCCNXM"
    for sample in [
        "14.0.0 (AP2A.240805.005, Aug 2024)",
        "BUILD/2026-11_RC1",
        "V" + "1.2.3." * 20,
    ]:
        out = canonical_version_str(sample)
        assert out == canonical_version_str(sample)
        assert 1 <= len(out) <= 64
        assert out.endswith("-" + hashlib.sha256(sample.encode()).hexdigest()[:12])

    with pytest.raises(ValueError, match="non-empty"):
        canonical_version_str("   ")


def test_publish_spec_v1_frozen_serialization_and_deterministic_hashes():
    spec = PublishSpecV1(
        device=_sample_device(),
        release=_sample_release(model_code="   "),
        enrichment=_sample_enrichment(),
    )

    with pytest.raises(FrozenInstanceError):
        spec.spec_version = 2  # type: ignore[misc]

    # Blank model_code falls back to device_codename just like 0006 idx_fw_identity
    assert spec.identity_tuple == ("husky", "husky", "Global", "AP2A.240805.005", "factory_zip")
    assert spec.identity_key == '["husky","husky","Global","AP2A.240805.005","factory_zip"]'

    # JSONL round-trip preserves exact equality and hashes
    jsonl_line = json.dumps(spec.to_dict(), separators=(",", ":"))
    restored = PublishSpecV1.from_dict(json.loads(jsonl_line))
    assert restored == spec
    assert restored.spec_sha256 == spec.spec_sha256
    assert restored.enrichment_sha256 == spec.enrichment_sha256

    # spec_sha256 covers (spec_version, device, release) only, independent of enrichment
    spec_without_enrichment = PublishSpecV1(
        device=_sample_device(),
        release=_sample_release(model_code="   "),
        enrichment=None,
    )
    assert spec_without_enrichment.spec_sha256 == spec.spec_sha256
    assert spec_without_enrichment.enrichment_sha256 is None
    assert spec_without_enrichment.to_firmware_metadata_dict() is None

    # enrichment_sha256 matches canonical sorted-key JSON of firmware_metadata schema 1
    meta_dict = spec.to_firmware_metadata_dict()
    assert meta_dict is not None
    assert meta_dict["schema"] == 1
    assert meta_dict["vendor"] == "google"
    expected_meta_hash = hashlib.sha256(
        json.dumps(meta_dict, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    assert spec.enrichment_sha256 == expected_meta_hash


@pytest.mark.parametrize(
    "device_kw,release_kw,enrichment_kw,err_match",
    [
        ({"codename": "marble"}, {"device_codename": "husky"}, None, "must match release.device_codename"),
        ({"codename": "__global__"}, {"device_codename": "__global__"}, None, "__global__"),
        ({"codename": "_leading"}, {"device_codename": "_leading"}, None, "device.codename"),
        ({"codename": "UpperCase"}, {"device_codename": "UpperCase"}, None, "device.codename"),
        ({"codename": "a" * 65}, {"device_codename": "a" * 65}, None, "device.codename"),
        ({"brand_id": "huawei"}, {}, None, "Restricted brand_id"),
        ({"brand_id": "honor"}, {}, None, "Restricted brand_id"),
        ({"brand_id": "HUAWEI"}, {}, None, "Restricted brand_id"),
        ({"brand_id": "nokia_unknown"}, {}, None, "Unsupported brand_id"),
        ({"boot_partition_type": "vendor_boot"}, {}, None, "boot_partition_type"),
        ({}, {"package_type": "ota_tar"}, None, "package_type"),
        ({}, {"version_str": ""}, None, "version_str"),
        ({}, {"version_str": "x" * 65}, None, "version_str"),
        ({}, {"version_str": "has space"}, None, "version_str"),
        ({}, {"original_official_url": "http://dl.google.com/a.zip"}, None, "original_official_url"),
        ({}, {"fast_cdn_url": "https://dl.google.com/a b.zip"}, None, "fast_cdn_url"),
        ({}, {"fast_cdn_url": "https://dl.google.com/a\x00.zip"}, None, "fast_cdn_url"),
        ({}, {"md5_or_sha256": "ABCDEF" + "0" * 26}, None, "md5_or_sha256"),
        ({}, {"md5_or_sha256": "a" * 40}, None, "md5_or_sha256"),
        ({}, {"published_date": "2024/08/05"}, None, "published_date"),
        ({}, {"verification_status": "UNVERIFIED"}, None, "verification_status"),
        ({}, {}, {"security_patch": "Aug 2024"}, "security_patch"),
    ],
)
def test_publish_spec_v1_strict_validation_rejects_invalid_inputs(
    device_kw, release_kw, enrichment_kw, err_match
):
    with pytest.raises(ValueError, match=err_match):
        PublishSpecV1(
            device=_sample_device(**device_kw),
            release=_sample_release(**release_kw),
            enrichment=_sample_enrichment(**enrichment_kw) if enrichment_kw is not None else None,
        )


@pytest.mark.parametrize(
    "status",
    ["VERIFIED_IMMUTABLE", "VERIFIED_RESOLVER", "VERIFIED_R2_BUNDLE"],
)
def test_publish_spec_v1_accepts_all_valid_verification_statuses_and_slash_version(status):
    spec = PublishSpecV1(
        device=_sample_device(brand_id="samsung", codename="dm3q", boot_partition_type="boot"),
        release=_sample_release(
            device_codename="dm3q",
            model_code="SM-S918B",
            version_str="S918BXXS6CXFE/S918BOXM6CXFE/S918BXXS6CXFE",
            package_type="samsung_encrypted",
            md5_or_sha256="f" * 32,
            verification_status=status,
            delivery_mode="samsung_fus",
            resolver_meta={"logic_check": "ok"},
        ),
    )
    assert spec.identity_tuple[1] == "SM-S918B"
    assert spec.release.verification_status == status


# ---------------------------------------------------------------------------
# 3. D1QuotaExceededError & Retry / Fast-Fail Semantics
# ---------------------------------------------------------------------------


def test_is_d1_quota_error_detection():
    assert issubclass(D1QuotaExceededError, D1BatchError)
    assert is_d1_quota_error("D1_ERROR: code 7500") is True
    assert is_d1_quota_error("plain error", error_codes=[7500]) is True
    assert is_d1_quota_error("plain error", status_code=429) is True
    assert is_d1_quota_error("You have exceeded your daily write allocation") is True
    assert is_d1_quota_error("daily read limit reached") is True
    assert is_d1_quota_error("Account D1 quota exhausted") is True
    assert is_d1_quota_error("Maximum rows written exceeded for today") is True
    assert is_d1_quota_error("queries limit exceeded") is True

    assert is_d1_quota_error("UNIQUE constraint failed: stock_firmwares.public_token") is False
    assert is_d1_quota_error("no such column: foo", status_code=400, error_codes=[7400]) is False
    assert (
        is_d1_quota_error(
            "{'code': 7500, 'message': 'not authorized: SQLITE_AUTH'}",
            status_code=400,
            error_codes=[7500],
        )
        is False
    )
    assert (
        is_d1_quota_error(
            "{'code': 7500, 'message': 'SQLITE_CONSTRAINT: UNIQUE constraint failed'}",
            status_code=400,
            error_codes=[7500],
        )
        is False
    )
    assert (
        is_d1_quota_error(
            "{'code': 7500, 'message': 'no such table: stock_firmwares_missing'}",
            status_code=400,
            error_codes=[7500],
        )
        is False
    )
    assert (
        is_d1_quota_error(
            "{'code': 7500, 'message': 'not authorized; daily write quota exceeded'}",
            status_code=400,
            error_codes=[7500],
        )
        is True
    )


def test_d1_client_http_400_7500_sql_error_raises_batch_error_without_sleep_retry():
    client = D1Client(cf_account_id="acc", cf_api_token="tok", cf_database_id="db")

    resp_auth = MagicMock(status_code=400, headers={})
    resp_auth.text = '{"messages":[],"result":[],"success":false,"errors":[{"code":7500,"message":"not authorized: SQLITE_AUTH"}]}'
    resp_auth.json.return_value = {
        "messages": [],
        "result": [],
        "success": False,
        "errors": [{"code": 7500, "message": "not authorized: SQLITE_AUTH"}],
    }
    resp_auth.raise_for_status.side_effect = requests.exceptions.HTTPError(
        "400 Client Error: Bad Request", response=resp_auth
    )

    with patch("fmr_core.d1_client.time.sleep") as mock_sleep:
        with patch.object(client.session, "post", return_value=resp_auth) as mock_post:
            with pytest.raises(D1BatchError) as exc_info:
                client.executemany(
                    "INSERT OR IGNORE INTO t VALUES (?)",
                    [(1,), (2,)],
                )
            assert not isinstance(exc_info.value, D1QuotaExceededError)
            assert "SQLITE_AUTH" in str(exc_info.value)
            assert mock_post.call_count == 1
            assert mock_sleep.call_count == 0

    client.close()


def test_d1_client_never_sleep_retries_on_7500_or_daily_allocation():
    client = D1Client(cf_account_id="acc", cf_api_token="tok", cf_database_id="db")

    # Case 1: HTTP 200 JSON payload reporting Cloudflare error code 7500
    resp_7500 = MagicMock(status_code=200)
    resp_7500.raise_for_status.return_value = None
    resp_7500.json.return_value = {
        "success": False,
        "errors": [{"code": 7500, "message": "D1_ERROR: exceeded daily write allocation"}],
    }

    with patch("fmr_core.d1_client.time.sleep") as mock_sleep:
        with patch.object(client.session, "post", return_value=resp_7500) as mock_post:
            with pytest.raises(D1QuotaExceededError) as exc_info:
                client.query("SELECT 1")
            assert mock_post.call_count == 1
            assert mock_sleep.call_count == 0
            assert 7500 in exc_info.value.error_codes

    # Case 2: HTTP 400 / 500 response carrying daily write allocation text
    resp_alloc = MagicMock(status_code=500, headers={})
    resp_alloc.text = "D1_ERROR: You have exceeded your daily write allocation"
    resp_alloc.json.return_value = {
        "success": False,
        "errors": [{"code": 7500, "message": "exceeded your daily write allocation"}],
    }
    resp_alloc.raise_for_status.side_effect = requests.exceptions.HTTPError(
        "500 Server Error", response=resp_alloc
    )

    with patch("fmr_core.d1_client.time.sleep") as mock_sleep:
        with patch.object(client.session, "post", return_value=resp_alloc) as mock_post:
            # Caught via D1BatchError too for 100% caller compatibility
            with pytest.raises(D1BatchError) as batch_exc:
                client.executemany(
                    "INSERT OR IGNORE INTO t VALUES (?)",
                    [(1,), (2,)],
                )
            assert isinstance(batch_exc.value, D1QuotaExceededError)
            assert mock_post.call_count == 1
            assert mock_sleep.call_count == 0

    client.close()


def test_d1_client_429_short_retry_after_vs_immediate_stop():
    client = D1Client(cf_account_id="acc", cf_api_token="tok", cf_database_id="db")

    def make_429(retry_after: str | None, body_msg: str = "Too Many Requests"):
        m = MagicMock(status_code=429)
        m.headers = {"Retry-After": retry_after} if retry_after is not None else {}
        m.text = body_msg
        m.json.return_value = {"success": False, "errors": [{"code": 10000, "message": body_msg}]}
        m.raise_for_status.side_effect = requests.exceptions.HTTPError("429 Too Many Requests", response=m)
        return m

    ok_resp = MagicMock(status_code=200)
    ok_resp.raise_for_status.return_value = None
    ok_resp.json.return_value = {
        "success": True,
        "result": [{"success": True, "results": [{"ok": 1}], "meta": {"changes": 0}}],
    }

    # A. 429 with Retry-After <= 2s retries briefly and succeeds
    with patch("fmr_core.d1_client.time.sleep") as mock_sleep:
        with patch.object(client.session, "post", side_effect=[make_429("0.5"), ok_resp]) as mock_post:
            rows = client.query("SELECT 1 AS ok")
            assert rows == [{"ok": 1}]
            assert mock_post.call_count == 2
            mock_sleep.assert_called_once_with(0.5)

    # B. 429 with Retry-After <= 2s raises D1QuotaExceededError once bounded retries exhaust
    with patch("fmr_core.d1_client.time.sleep") as mock_sleep:
        with patch.object(
            client.session,
            "post",
            side_effect=[make_429("1"), make_429("1"), make_429("1")],
        ) as mock_post:
            with pytest.raises(D1QuotaExceededError) as exc_info:
                client.query("SELECT 1 AS ok")
            assert exc_info.value.status_code == 429
            assert mock_post.call_count == 3
            assert mock_sleep.call_count == 2

    # C. 429 with Retry-After > 2s fails immediately without sleeping
    with patch("fmr_core.d1_client.time.sleep") as mock_sleep:
        with patch.object(client.session, "post", return_value=make_429("30")) as mock_post:
            with pytest.raises(D1QuotaExceededError):
                client.query("SELECT 1 AS ok")
            assert mock_post.call_count == 1
            assert mock_sleep.call_count == 0

    # D. 429 with Retry-After <= 2s BUT daily quota message fails immediately without sleeping
    with patch("fmr_core.d1_client.time.sleep") as mock_sleep:
        with patch.object(
            client.session,
            "post",
            return_value=make_429("1", body_msg="exceeded daily write allocation"),
        ) as mock_post:
            with pytest.raises(D1QuotaExceededError):
                client.query("SELECT 1 AS ok")
            assert mock_post.call_count == 1
            assert mock_sleep.call_count == 0

    client.close()


def test_d1_client_multi_chunk_quota_error_preserves_partial_progress():
    client = D1Client(cf_account_id="acc", cf_api_token="tok", cf_database_id="db")

    chunk1_ok = MagicMock(status_code=200)
    chunk1_ok.raise_for_status.return_value = None
    chunk1_ok.json.return_value = {
        "success": True,
        "result": [
            {"success": True, "meta": {"changes": 1}},
            {"success": True, "meta": {"changes": 1}},
        ],
    }

    chunk2_quota = MagicMock(status_code=200)
    chunk2_quota.raise_for_status.return_value = None
    chunk2_quota.json.return_value = {
        "success": False,
        "errors": [{"code": 7500, "message": "D1_ERROR: daily write quota exceeded"}],
    }

    with patch("fmr_core.d1_client.time.sleep") as mock_sleep:
        with patch.object(client.session, "post", side_effect=[chunk1_ok, chunk2_quota]) as mock_post:
            with pytest.raises(D1QuotaExceededError) as exc_info:
                client.executemany(
                    "INSERT OR IGNORE INTO t VALUES (?)",
                    [(1,), (2,), (3,), (4,)],
                    max_batch_size=2,
                )
            assert mock_post.call_count == 2
            assert mock_sleep.call_count == 0
            assert exc_info.value.executed_chunks == 1
            assert exc_info.value.total_changes == 2
            assert exc_info.value.offset == 2

    client.close()


# ---------------------------------------------------------------------------
# 4. Schema Migration v7 (0007_firmware_delivery_specs.sql) & Cleanup
# ---------------------------------------------------------------------------


def test_identity_maintenance_artifacts_deleted():
    assert not (RUNNER_ROOT / "fmr_core" / "identity_maintenance.py").exists()
    assert not (RUNNER_ROOT / "tests" / "export_identity_batches.py").exists()
    assert not (RUNNER_ROOT / "tests" / "fixtures" / "d1_identity_batches.json").exists()


def test_ensure_schema_through_v7_creates_and_verifies_delivery_specs(tmp_path, monkeypatch):
    assert CURRENT == 7
    sqlite_path = tmp_path / "schema_v7.sqlite"
    db = D1Client(local_sqlite_path=str(sqlite_path))
    try:
        db.execute_script((RUNNER_ROOT / "schema.sql").read_text(encoding="utf-8"))
        ensure_base_brands_and_anchor(db)

        # Default through=5 stays preserved for legacy read-only callers
        with pytest.raises(RuntimeError, match="migration required"):
            ensure_schema(db)
        ensure_schema(db, allow_migration=True)
        assert [r["version"] for r in db.query("SELECT version FROM fmr_schema_migrations ORDER BY version")] == [4, 5]

        # Upgrading through=7 applies 0006 and 0007 cleanly
        ensure_schema(db, allow_migration=True, through=7)
        assert [r["version"] for r in db.query("SELECT version FROM fmr_schema_migrations ORDER BY version")] == [
            4,
            5,
            6,
            7,
        ]

        # Insert valid delivery spec and verify json_valid CHECK constraint
        db.execute(
            """INSERT INTO firmware_delivery_specs(public_token, delivery_mode, resolver_json, content_hash)
               VALUES (?, ?, ?, ?)""",
            ("fw_test001", "samsung_fus", '{"model":"SM-S918B","region":"EUX"}', "h1"),
        )
        rows = db.query("SELECT public_token, delivery_mode, content_hash FROM firmware_delivery_specs")
        assert rows == [{"public_token": "fw_test001", "delivery_mode": "samsung_fus", "content_hash": "h1"}]

        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                """INSERT INTO firmware_delivery_specs(public_token, delivery_mode, resolver_json, content_hash)
                   VALUES (?, ?, ?, ?)""",
                ("fw_badjson", "samsung_fus", "{not-valid-json", "h2"),
            )

        indices = {
            r["name"]
            for r in db.query("PRAGMA index_list(firmware_delivery_specs)")
            if not r["name"].startswith("sqlite_autoindex_")
        }
        assert "idx_delivery_specs_hash" in indices

        # Re-verification when _fmr_schema_ready is reset succeeds without re-applying DDL
        db._fmr_schema_ready = 0
        ensure_schema(db, allow_migration=False, through=7)
        assert db._fmr_schema_ready == 7

        # Dropping idx_delivery_specs_hash triggers verification failure on next check
        db.execute("DROP INDEX idx_delivery_specs_hash")
        db._fmr_schema_ready = 0
        with pytest.raises(RuntimeError, match="idx_delivery_specs_hash"):
            ensure_schema(db, allow_migration=False, through=7)
    finally:
        db.close()

    # Verify CLI main() defaults --through to CURRENT (7)
    fresh_path = tmp_path / "cli_v7.sqlite"
    fresh_db = D1Client(local_sqlite_path=str(fresh_path))
    fresh_db.execute_script((RUNNER_ROOT / "schema.sql").read_text(encoding="utf-8"))
    fresh_db.close()

    monkeypatch.setenv("LOCAL_SQLITE_PATH", str(fresh_path))
    monkeypatch.setattr(sys, "argv", ["schema_migrations", "--apply"])
    migrations_main()

    verify_db = D1Client(local_sqlite_path=str(fresh_path))
    try:
        versions = [r["version"] for r in verify_db.query("SELECT version FROM fmr_schema_migrations ORDER BY version")]
        assert versions == [4, 5, 6, 7]
    finally:
        verify_db.close()


def test_migration_mains_use_extended_d1_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fmr_core import d1_client as d1_client_mod
    from fmr_core import firmware_metadata_schema

    fresh_path = tmp_path / "cli_timeout.sqlite"
    seed_db = D1Client(local_sqlite_path=str(fresh_path))
    seed_db.execute_script((RUNNER_ROOT / "schema.sql").read_text(encoding="utf-8"))
    seed_db.close()

    recorded_timeouts: list[float] = []
    real_d1_client = d1_client_mod.D1Client

    def _recording_client(*args, **kwargs):
        recorded_timeouts.append(kwargs.get("timeout"))
        return real_d1_client(*args, **kwargs)

    monkeypatch.setenv("LOCAL_SQLITE_PATH", str(fresh_path))
    monkeypatch.setattr(d1_client_mod, "D1Client", _recording_client)

    monkeypatch.setattr(sys, "argv", ["schema_migrations", "--apply"])
    migrations_main()

    monkeypatch.setattr(sys, "argv", ["firmware_metadata_schema", "--apply"])
    firmware_metadata_schema.main()

    assert recorded_timeouts == [180.0, 180.0]


def test_ensure_schema_skips_internal_cf_and_sqlite_tables(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sqlite_path = tmp_path / "cf_kv.sqlite"
    db = D1Client(local_sqlite_path=str(sqlite_path))
    try:
        db.execute_script((RUNNER_ROOT / "schema.sql").read_text(encoding="utf-8"))
        ensure_base_brands_and_anchor(db)
        # Create an actual _cf_KV table so it appears in sqlite_master alongside sqlite_* tables
        db.execute("CREATE TABLE _cf_KV (key TEXT PRIMARY KEY, value BLOB)")

        orig_query = db.query

        def guarded_query(sql: str, params=()):
            normalized = sql.strip().lower()
            if normalized == "select name from sqlite_master where type='table'":
                rows = orig_query(sql, params)
                names = {r["name"] for r in rows}
                if "sqlite_sequence" not in names:
                    rows = list(rows) + [{"name": "sqlite_sequence"}]
                return rows
            if "pragma foreign_key_list" in normalized and (
                '"_cf_kv"' in normalized or '"sqlite_sequence"' in normalized
            ):
                raise RuntimeError("not authorized: SQLITE_AUTH")
            return orig_query(sql, params)

        monkeypatch.setattr(db, "query", guarded_query)

        ensure_schema(db, allow_migration=True, through=7)
        versions = [r["version"] for r in orig_query("SELECT version FROM fmr_schema_migrations ORDER BY version")]
        assert versions == [4, 5, 6, 7]
    finally:
        db.close()

