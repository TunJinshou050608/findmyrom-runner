"""Comprehensive offline unit and end-to-end tests for the v4 stock-firmware pipeline,
Google Play official CSV parser, Station #1 Google official adapter, RangeProbeVerifier,
atomic Outbox, and Quota-Breakpoint D1 Publisher.

Strictly offline: uses httpx.MockTransport and local SQLite D1Client only.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any

import httpx
import pytest

from fmr_core.crypto import (
    build_firmware_payload,
    generate_keypair,
    generate_public_token,
    sign_firmware,
    verify_signature,
)
from fmr_core.d1_client import D1Client, D1QuotaExceededError
from fmr_core import firmware_metadata_schema
from fmr_core.schema_migrations import ensure_schema
from fmr_ingest_stock.adapters import (
    AdapterDevice,
    AdapterRelease,
    GoogleAdapter,
    OfficialMetadataHTTP,
    VendorAdapter,
    group_play_devices_by_codename,
    load_vendor_configs,
    parse_google_play_devices_csv,
)
from fmr_ingest_stock.cdn_rewriter import OFFICIAL_HOST_MAP, validate_official_download_url
from fmr_ingest_stock.pipeline import (
    DEAD_UPSTREAM_TTL_SECONDS,
    OutboxSpool,
    RangeProbeVerifier,
    UnofficialDownloadURLError,
    acquire_file_lock,
    get_pipeline_status,
    run_collector,
    run_publisher,
)
from fmr_ingest_stock.pipeline.cli import main as pipeline_cli_main

RUNNER_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_SQL_PATH = RUNNER_ROOT / "fmr_core" / "schema.sql"


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _init_migrated_d1(sqlite_path: Path) -> D1Client:
    db = D1Client(local_sqlite_path=str(sqlite_path))
    db.execute_script(SCHEMA_SQL_PATH.read_text(encoding="utf-8"))
    ensure_schema(db, allow_migration=True, through=7)
    firmware_metadata_schema.ensure(db, apply=True)
    return db


class CountingD1Wrapper:
    """Transparent proxy around D1Client counting reads (`query`) and writes (`execute_batch`)
    and optionally injecting D1QuotaExceededError (error 7500) on a target batch call."""

    def __init__(self, inner: D1Client, *, fail_on_batch_number: int | None = None) -> None:
        self._inner = inner
        self.query_calls = 0
        self.batch_calls = 0
        self.fail_on_batch_number = fail_on_batch_number

    @property
    def mode(self) -> str:
        return self._inner.mode

    def reset_counts(self) -> None:
        self.query_calls = 0
        self.batch_calls = 0

    def query(self, sql: str, params: Any = ()) -> list[dict[str, Any]]:
        self.query_calls += 1
        return self._inner.query(sql, params)

    def execute_batch(self, statements: Any) -> dict[str, Any]:
        self.batch_calls += 1
        if self.fail_on_batch_number is not None and self.batch_calls == self.fail_on_batch_number:
            raise D1QuotaExceededError(
                "Cloudflare D1 daily write quota exceeded (code 7500)",
                offset=0,
                failed_sql=statements[0][0] if statements else "<unknown>",
                error={"code": 7500, "message": "D1 quota exceeded"},
                status_code=429,
                error_codes=(7500,),
            )
        return self._inner.execute_batch(statements)


# ---------------------------------------------------------------------------
# 1. Pure-Official Host Allowlist & Declarative Vendor Config Tests
# ---------------------------------------------------------------------------


def test_zero_third_party_mirrors_in_cdn_rewriter_and_vendors_yaml():
    assert ("mirrors.loli" + "net.com") not in OFFICIAL_HOST_MAP["motorola"]
    assert set(OFFICIAL_HOST_MAP["motorola"]) == {
        "android.googleapis.com",
        "lenovo.com",
        "motorola.com",
    }

    banned_third_party_urls = [
        ("motorola", "https://mirrors.loli" + "net.com/firmware/moto/rom.zip"),
        ("samsung", "https://fwat" + "las.com/samsung/SM-S928B/rom.zip"),
        ("xiaomi", "https://xmfirmware" + "updater.com/miui/houji/rom.zip"),
        ("asus", "https://igoreis" + "berg.com/asus/AI2401/rom.zip"),
        ("oneplus", "https://otacheck" + "er.com/oneplus/rom.zip"),
        ("google", "https://roms.danielsprin" + "ger.at/pixel/tokay.zip"),
    ]
    for brand, bad_url in banned_third_party_urls:
        with pytest.raises(ValueError):
            validate_official_download_url(brand, bad_url)
        with pytest.raises(ValueError):
            validate_official_download_url("", bad_url)

    configs = load_vendor_configs()
    assert len(configs) == 17

    active_official_vendors = {
        "google",
        "samsung",
        "xiaomi",
        "oneplus",
        "oppo",
        "realme",
        "asus",
        "vivo",
        "tecno",
        "infinix",
        "itel",
        "sony",
    }
    for vid in active_official_vendors:
        assert configs[vid].enabled is True
        assert configs[vid].adapter == vid
        assert configs[vid].status == "active_official"
        assert configs[vid].tier == "official"
        assert configs[vid].bucket is not None and 0 <= configs[vid].bucket <= 11
    assert configs["google"].bucket == 0

    expected_disabled_statuses = {
        "rog": ("covered_by_asus_adapter", "official"),
        "motorola": ("requires_lmsa_oauth_token", "official"),
        "nothing": ("requires_device_authenticated_checkin", "official"),
        "huawei": ("suppressed_closed_bootloader", "suppressed"),
        "honor": ("suppressed_closed_bootloader", "suppressed"),
    }
    assert set(configs.keys()) == active_official_vendors | set(expected_disabled_statuses.keys())
    for vid, (expected_status, expected_tier) in expected_disabled_statuses.items():
        assert configs[vid].enabled is False
        assert configs[vid].status == expected_status
        assert configs[vid].tier == expected_tier
        if expected_tier == "suppressed":
            assert configs[vid].bucket is None
            assert configs[vid].adapter is None
        else:
            assert configs[vid].bucket is not None and 0 <= configs[vid].bucket <= 11

    # Ensure unsafe yaml loader is never used in new modules
    unsafe_token = "yaml." + "load("
    for folder in (
        RUNNER_ROOT / "fmr_ingest_stock" / "adapters",
        RUNNER_ROOT / "fmr_ingest_stock" / "pipeline",
    ):
        for py_file in folder.glob("*.py"):
            assert unsafe_token not in py_file.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 2. OfficialMetadataHTTP Security & 24 MiB SQLite Cache Tests
# ---------------------------------------------------------------------------


def test_official_metadata_http_allowlist_binary_block_and_large_304_cache(tmp_path: Path):
    large_html = ("<html><body>" + ("Pixel-Row-Data-" * 120_000) + "</body></html>").encode("utf-8")
    assert len(large_html) > 1_500_000  # Exceeds old 1 MB silent cache cliff!

    call_log: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        call_log.append(dict(request.headers))
        if request.headers.get("if-none-match") == '"etag-v1"':
            return httpx.Response(304, headers={"ETag": '"etag-v1"'})
        return httpx.Response(
            200,
            headers={"ETag": '"etag-v1"', "Content-Type": "text/html; charset=utf-8"},
            content=large_html,
        )

    cache_db = tmp_path / "meta_cache.sqlite"
    with httpx.Client(transport=httpx.MockTransport(handler)) as raw_client:
        with OfficialMetadataHTTP(client=raw_client, cache_db_path=cache_db) as http:
            # Reject third-party hosts
            for bad_meta in [
                "https://fwat" + "las.com/api/devices",
                "https://xmfirmware" + "updater.com/data/devices.json",
                "https://mirrors.loli" + "net.com/firmware/",
            ]:
                with pytest.raises(ValueError, match="OFFICIAL_METADATA_HOSTS"):
                    http.get(bad_meta)

            # Reject binary ROM suffixes even on allowed hosts
            for bad_bin in [
                "https://developers.google.com/android/tokay.zip",
                "https://storage.googleapis.com/play_public/archive.tgz",
                "https://fota-cloud-dn.ospserver.net/firmware.enc4",
            ]:
                with pytest.raises(ValueError, match="Binary ROM suffix"):
                    http.get(bad_bin)

            # First fetch stores >1.5 MiB body in zlib SQLite cache
            r1 = http.get(
                "https://developers.google.com/android/ota?hl=en",
                cookies={"devsite_wall_acks": "nexus-ota-tos"},
            )
            assert r1.from_cache is False
            assert r1.content == large_html

            # Second fetch sends If-None-Match and restores full >1.5 MiB body on 304
            r2 = http.get(
                "https://developers.google.com/android/ota?hl=en",
                cookies={"devsite_wall_acks": "nexus-ota-tos"},
            )
            assert r2.from_cache is True
            assert r2.content == large_html
            assert call_log[1].get("if-none-match") == '"etag-v1"'


# ---------------------------------------------------------------------------
# 3. Google Play Official `supported_devices.csv` Tests
# ---------------------------------------------------------------------------


def test_google_play_devices_utf16le_and_utf8_filtering():
    csv_text = (
        "Retail Branding,Marketing Name,Device,Model\n"
        "Google,Pixel 9,tokay,G2YBB\n"
        "Google,Pixel 9,tokay,GUR25\n"
        "Google,Pixel 9,tokay,G2YBB\n"  # duplicate pair
        "Google,Pixel Tablet,tangorpro,GTU8P\n"
        "Google,Chromecast with Google TV,sabrina,GZRNL\n"
        "Google,ADT-3 Android TV,adt3,ADT-3\n"
        "Google,Pixelbook Go Chromebook,atlas_cheets,Pixelbook Go\n"
        "Google,ChromeOS Hatch,hatch_cheets,Hatch\n"
        "Google,Pixel Watch 3,eos,GBDU9\n"
        "Google,Google TV Streamer Box,kirkwood,GRS6B\n"
        "Google,HDMI Streaming Dongle,dongle1,GDNGL\n"
        "Samsung,Galaxy S24 Ultra,e3q,SM-S928B\n"
        "Samsung,The Freestyle Smart TV Projector,proj1,SP-LSP3B\n"
    )
    utf16le_bytes = b"\xff\xfe" + csv_text.encode("utf-16-le")
    utf8_bom_bytes = b"\xef\xbb\xbf" + csv_text.encode("utf-8")

    records_16 = parse_google_play_devices_csv(utf16le_bytes, brands="Google")
    records_8 = parse_google_play_devices_csv(utf8_bom_bytes, brands="google")
    assert records_16 == records_8

    grouped = group_play_devices_by_codename(records_16)
    assert set(grouped.keys()) == {"tangorpro", "tokay"}
    assert grouped["tokay"].models == ("G2YBB", "GUR25")
    assert grouped["tangorpro"].models == ("GTU8P",)

    # Enabling wearables includes Pixel Watch 3 while still excluding TV/Chromebook/Box/Dongle
    with_watch = group_play_devices_by_codename(
        parse_google_play_devices_csv(utf16le_bytes, brands="google", include_wearables=True)
    )
    assert set(with_watch.keys()) == {"eos", "tangorpro", "tokay"}


# ---------------------------------------------------------------------------
# 4. Station #1 GoogleAdapter Parsing, Dates, Versions & Partial-Page Guard
# ---------------------------------------------------------------------------


DEVSITE_OTA_HTML_FIXTURE = f"""<!DOCTYPE html>
<html>
<body>
  <h2 id="legal">Legal Notice</h2>
  <h2 id="instructions">Updating Pixel Devices</h2>

  <h2 id="tokay" data-text='"tokay" for Pixel 9'>"tokay" for Pixel 9</h2>
  <table>
    <tr><th>Version</th><th>Download</th><th>SHA-256 Checksum</th></tr>
    <tr>
      <td>15.0.0 (AD1A.240905.004, Sep 2024)</td>
      <td><a href="https://dl.google.com/dl/android/aosp/tokay-ota-ad1a.240905.004-11223344.zip">Link</a></td>
      <td>{_sha("tokay-ota-15")}</td>
    </tr>
    <tr>
      <td>14.0.0 (AP2A.240805.005.S4, Aug 2024, EMEA)</td>
      <td><a href="https://dl.google.com/dl/android/aosp/tokay-ota-ap2a.240805.005.s4-55667788.zip">Link</a></td>
      <td>{_sha("tokay-ota-emea")}</td>
    </tr>
    <tr>
      <td>14.0.0 (O2, 4G, UQ1A.240205.004.B1, Feb 2024, UK)</td>
      <td><a href="https://dl.google.com/dl/android/aosp/custom_carrier_ota_package.zip">Link</a></td>
      <td>{_sha("tokay-ota-o2")}</td>
    </tr>
    <tr>
      <td>Developer Preview 2 (SPP2.210219.008, Mar 2021)</td>
      <td><a href="https://dl.google.com/dl/android/aosp/tokay-ota-spp2.210219.008-99aabbcc.zip">Link</a></td>
      <td>{_sha("tokay-ota-dp2")}</td>
    </tr>
  </table>

  <h3 id="bluejay">"bluejay" for Pixel 6a</h3>
  <table>
    <tr><th>Version</th><th>Download</th><th>SHA-256 Checksum</th></tr>
    <tr>
      <td>12L (SP2A.220505.002, May 2022)</td>
      <td><a href="https://dl.google.com/dl/android/aosp/bluejay-ota-sp2a.220505.002-deadbeef.zip">Link</a></td>
      <td>{_sha("bluejay-ota-12l")}</td>
    </tr>
    <tr>
      <td>14.0.0 (AP2A.240805.005, Aug 2024)</td>
      <td><a href="https://dl.google.com/dl/android/aosp/bluejay-ota-ap2a.240805.005-cafebabe.zip">Link</a></td>
      <td>{_sha("bluejay-ota-14")}</td>
    </tr>
  </table>
</body>
</html>
"""

DEVSITE_IMAGES_HTML_FIXTURE = f"""<!DOCTYPE html>
<html>
<body>
  <h2 id="terms">Terms and Conditions</h2>

  <h2 id="tokay">"tokay" for Pixel 9</h2>
  <table>
    <tr><th>Version</th><th>Flash</th><th>Download</th><th>SHA-256 Checksum</th></tr>
    <tr>
      <td>15.0.0 (AD1A.240905.004, Sep 2024)</td>
      <td><a href="https://flash.android.com/build/12345">Flash</a></td>
      <td><a href="https://dl.google.com/dl/android/aosp/tokay-ad1a.240905.004-factory-abcdef01.zip">Link</a></td>
      <td>{_sha("tokay-factory-15")}</td>
    </tr>
  </table>

  <h2 id="yakju">"yakju" for Galaxy Nexus "maguro" (GSM/HSPA+)</h2>
  <table>
    <tr><th>Version</th><th>Download</th><th>SHA-256 Checksum</th></tr>
    <tr>
      <td>4.3 (JWR66Y)</td>
      <td><a href="https://dl.google.com/dl/android/aosp/yakju-jwr66y-factory-09207065.tgz">Link</a></td>
      <td>{_sha("maguro-yakju-43")}</td>
    </tr>
  </table>

  <h4 id="takju">"takju" for Galaxy Nexus "maguro" (GSM/HSPA+) (with Google Wallet)</h4>
  <table>
    <tr><th>Version</th><th>Download</th><th>SHA-256 Checksum</th></tr>
    <tr>
      <td>4.3 (JWR66Y)</td>
      <td><a href="https://dl.google.com/dl/android/aosp/takju-jwr66y-factory-5104c132.tgz">Link</a></td>
      <td>{_sha("maguro-takju-43")}</td>
    </tr>
  </table>
</body>
</html>
"""


def test_google_adapter_full_parsing_variants_dates_and_atomic_guard(tmp_path: Path):
    def ok_handler(request: httpx.Request) -> httpx.Response:
        cookie_hdr = request.headers.get("cookie", "")
        if request.url.path == "/android/ota":
            assert "devsite_wall_acks=nexus-ota-tos" in cookie_hdr
            return httpx.Response(200, text=DEVSITE_OTA_HTML_FIXTURE)
        if request.url.path == "/android/images":
            assert "devsite_wall_acks=nexus-image-tos" in cookie_hdr
            return httpx.Response(200, text=DEVSITE_IMAGES_HTML_FIXTURE)
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(ok_handler)) as raw_client:
        with OfficialMetadataHTTP(client=raw_client) as http:
            adapter = GoogleAdapter()
            devices = {d.codename: d for d in adapter.discover_devices(http)}

            # Merged across BOTH ota (tokay, bluejay) and images (tokay, maguro), excluding legal/terms/instructions
            assert set(devices.keys()) == {"bluejay", "maguro", "tokay"}

            # Pixel 9: earliest observed major is 14 -> init_boot
            assert devices["tokay"].marketing_name == "Google Pixel 9"
            assert devices["tokay"].boot_partition_type == "init_boot"

            # Pixel 6a: earliest observed major is 12 (from 12L) -> boot!
            assert devices["bluejay"].marketing_name == "Google Pixel 6a"
            assert devices["bluejay"].boot_partition_type == "boot"

            # Galaxy Nexus maguro: preserves "Google " prefix, strips "(with Google Wallet)", merges yakju/takju
            assert devices["maguro"].marketing_name == "Google Galaxy Nexus (GSM/HSPA+)"
            assert devices["maguro"].model_numbers == ("yakju", "takju")
            assert devices["maguro"].boot_partition_type == "boot"

            tokay_rels = adapter.fetch_releases(devices["tokay"], http)
            by_key = {(r.version_str, r.package_type, r.region_code): r for r in tokay_rels}

            emea = by_key[("AP2A.240805.005.S4", "recovery_zip", "EMEA")]
            assert emea.android_version == "14.0.0"
            assert emea.published_date == "2024-08-01"
            assert emea.security_patch == "2024-08-05"

            # Fallback caption parser skips "O2" and "4G" and extracts UQ1A.240205.004.B1 cleanly!
            o2_rel = by_key[("UQ1A.240205.004.B1", "recovery_zip", "O2 · 4G · UK")]
            assert o2_rel.version_str == "UQ1A.240205.004.B1"
            assert o2_rel.published_date == "2024-02-01"
            assert o2_rel.security_patch == "2024-02-05"

            # Developer Preview caption never falls back to silent 'unknown'
            dp2_rel = by_key[("SPP2.210219.008", "recovery_zip", "Global")]
            assert dp2_rel.android_version == "Preview"
            assert dp2_rel.security_patch is None  # Non-release build prefix SPP2

            # Factory image parsed alongside OTA
            fac_rel = by_key[("AD1A.240905.004", "factory_zip", "Global")]
            assert fac_rel.android_version == "15.0.0"
            assert fac_rel.published_date == "2024-09-01"
            assert fac_rel.security_patch == "2024-09-05"

            # Pixel 6a 12L release
            bluejay_rels = {r.version_str: r for r in adapter.fetch_releases(devices["bluejay"], http)}
            rel_12l = bluejay_rels["SP2A.220505.002"]
            assert rel_12l.android_version == "12L"
            assert rel_12l.published_date == "2022-05-01"
            assert rel_12l.security_patch == "2022-05-05"

            # Nexus maguro yakju & takju distinct 5-tuple identities + fastboot_tgz
            maguro_rels = adapter.fetch_releases(devices["maguro"], http)
            assert len(maguro_rels) == 2
            assert {r.model_code for r in maguro_rels} == {"yakju", "takju"}
            assert all(r.package_type == "fastboot_tgz" for r in maguro_rels)
            assert len({r.identity_key for r in maguro_rels}) == 2

    # Atomic failure when second page returns a TOS wall (0 rows): raises RuntimeError & evicts cache!
    def tos_wall_images_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/android/ota":
            return httpx.Response(200, text=DEVSITE_OTA_HTML_FIXTURE)
        return httpx.Response(200, text="<html><body><h2>Acknowledge Terms</h2></body></html>")

    tos_cache_db = tmp_path / "tos_cache.sqlite"
    with httpx.Client(transport=httpx.MockTransport(tos_wall_images_handler)) as bad_client:
        with OfficialMetadataHTTP(client=bad_client, cache_db_path=tos_cache_db) as http:
            with pytest.raises(RuntimeError, match="yielded 0 valid releases"):
                GoogleAdapter().discover_devices(http)

    conn = sqlite3.connect(str(tos_cache_db))
    cached_urls = [r[0] for r in conn.execute("SELECT url FROM http_cache").fetchall()]
    conn.close()
    assert "https://developers.google.com/android/images?hl=en" not in cached_urls


# ---------------------------------------------------------------------------
# 5. RangeProbeVerifier Tests
# ---------------------------------------------------------------------------


def _sample_release(url: str, pkg: str = "recovery_zip") -> AdapterRelease:
    return AdapterRelease(
        device_codename="tokay",
        model_code="tokay",
        region_code="Global",
        version_str="AD1A.240905.004",
        android_version="15.0.0",
        package_type=pkg,
        original_official_url=url,
        fast_cdn_url=url,
        md5_or_sha256=_sha(url),
        published_date="2024-09-01",
    )


def test_range_probe_verifier_magic_sizes_soft404_429_and_redirects():
    zip_head = b"PK\x03\x04" + (b"\x14\x00" * 30)
    tgz_head = b"\x1f\x8b\x08\x00" + (b"\x00" * 60)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("range") == "bytes=0-63"
        path = request.url.path
        if path == "/good/ota.zip":
            return httpx.Response(
                206,
                headers={
                    "Content-Type": "application/octet-stream",
                    "Content-Range": "bytes 0-63/2555505869",
                },
                content=zip_head,
            )
        if path == "/good/factory.tgz":
            return httpx.Response(
                206,
                headers={
                    "Content-Type": "application/x-gtar",
                    "Content-Range": "bytes 0-63/886259712",
                },
                content=tgz_head,
            )
        if path == "/redirect/official.zip":
            return httpx.Response(
                302,
                headers={"Location": "https://dl.googleusercontent.com/good_target.zip"},
            )
        if path == "/good_target.zip":
            return httpx.Response(
                206,
                headers={
                    "Content-Type": "application/zip",
                    "Content-Range": "bytes 0-63/1073741824",
                },
                content=zip_head,
            )
        if path == "/redirect/evil.zip":
            return httpx.Response(
                302,
                headers={"Location": "https://mirrors.loli" + "net.com/evil.zip"},
            )
        if path == "/soft404.zip":
            return httpx.Response(
                200,
                headers={"Content-Type": "text/html; charset=UTF-8", "Content-Length": "99999999"},
                content=b"<!doctype html><html>Not Found</html>",
            )
        if path == "/ratelimited.zip":
            return httpx.Response(429, headers={"Retry-After": "60"})
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with RangeProbeVerifier(client=client, resolve_dns=False) as verifier:
            # 1. Valid 206 ZIP + Content-Range -> 2.38 GB
            ok_zip = verifier.verify_one("google", _sample_release("https://dl.google.com/good/ota.zip"))
            assert ok_zip.status == "VERIFIED"
            assert ok_zip.file_size_bytes == 2555505869
            assert ok_zip.file_size_str == "2.38 GB"

            # 2. Valid 206 TGZ -> 845.2 MB
            ok_tgz = verifier.verify_one(
                "google",
                _sample_release("https://dl.google.com/good/factory.tgz", pkg="fastboot_tgz"),
            )
            assert ok_tgz.status == "VERIFIED"
            assert ok_tgz.file_size_str == "845.2 MB"

            # 3. Official redirect (dl.google.com -> dl.googleusercontent.com) succeeds & keeps stable fast_cdn_url
            ok_redir = verifier.verify_one(
                "google", _sample_release("https://dl.google.com/redirect/official.zip")
            )
            assert ok_redir.status == "VERIFIED"
            assert ok_redir.release.fast_cdn_url == "https://dl.google.com/redirect/official.zip"
            assert ok_redir.file_size_str == "1.00 GB"

            # 4. Redirect to third-party host rejected!
            bad_redir = verifier.verify_one(
                "google", _sample_release("https://dl.google.com/redirect/evil.zip")
            )
            assert bad_redir.status == "DEAD_UPSTREAM"
            assert bad_redir.rejected_unofficial is True
            with pytest.raises(UnofficialDownloadURLError):
                verifier.verify_one(
                    "google",
                    _sample_release("https://dl.google.com/redirect/evil.zip"),
                    raise_on_unofficial=True,
                )

            # 5. Direct third-party URL rejected!
            bad_direct = verifier.verify_one(
                "motorola", _sample_release("https://mirrors.loli" + "net.com/moto/rom.zip")
            )
            assert bad_direct.status == "DEAD_UPSTREAM"
            assert bad_direct.rejected_unofficial is True

            # 6. HTML soft-404 -> DEAD_UPSTREAM with 7-day negative cache TTL
            soft_404 = verifier.verify_one(
                "google", _sample_release("https://dl.google.com/soft404.zip")
            )
            assert soft_404.status == "DEAD_UPSTREAM"
            assert soft_404.retry_after_seconds == DEAD_UPSTREAM_TTL_SECONDS

            # 7. HTTP 429 -> DEFERRED_VERIFY with exponential backoff
            deferred_0 = verifier.verify_one(
                "google",
                _sample_release("https://dl.google.com/ratelimited.zip"),
                failure_count=0,
            )
            deferred_2 = verifier.verify_one(
                "google",
                _sample_release("https://dl.google.com/ratelimited.zip"),
                failure_count=2,
            )
            assert deferred_0.status == "DEFERRED_VERIFY"
            assert deferred_2.status == "DEFERRED_VERIFY"
            assert deferred_2.retry_after_seconds > deferred_0.retry_after_seconds


# ---------------------------------------------------------------------------
# 6. End-to-End Collector -> Outbox -> Publisher, Bootstrap, Quota & Collisions
# ---------------------------------------------------------------------------


def _make_range_mock_client() -> httpx.Client:
    zip_head = b"PK\x03\x04" + (b"\x00" * 60)
    tgz_head = b"\x1f\x8b\x08\x00" + (b"\x00" * 60)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(".tgz"):
            return httpx.Response(
                206,
                headers={
                    "Content-Type": "application/x-gtar",
                    "Content-Range": f"bytes 0-63/{420 * 1024 * 1024}",
                },
                content=tgz_head,
            )
        return httpx.Response(
            206,
            headers={
                "Content-Type": "application/zip",
                "Content-Range": f"bytes 0-63/{2 * 1024 * 1024 * 1024}",
            },
            content=zip_head,
        )

    return httpx.Client(transport=httpx.MockTransport(handler))


def _make_google_meta_mock_client() -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/android/ota":
            return httpx.Response(200, text=DEVSITE_OTA_HTML_FIXTURE)
        if request.url.path == "/android/images":
            return httpx.Response(200, text=DEVSITE_IMAGES_HTML_FIXTURE)
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_e2e_collector_outbox_publisher_signatures_zero_noop_and_bootstrap(tmp_path: Path):
    state_dir = tmp_path / "state"
    d1_sqlite = tmp_path / "d1.sqlite"
    db = _init_migrated_d1(d1_sqlite)
    priv_key, pub_key = generate_keypair()

    # Pre-seed an existing firmware row in D1 BEFORE first Publisher run to verify bootstrap token/sig preservation!
    preexisting_token = generate_public_token("fw")
    preexisting_url = "https://dl.google.com/dl/android/aosp/tokay-ota-ad1a.240905.004-11223344.zip"
    preexisting_sig = sign_firmware(preexisting_token, preexisting_url, priv_key)
    db.execute(
        "INSERT INTO brands (id, name, bl_unlock_status) VALUES ('google', 'Google', 'unlockable')"
    )
    db.execute(
        "INSERT INTO devices (codename, brand_id, marketing_name, boot_partition_type) "
        "VALUES ('tokay', 'google', 'Google Pixel 9', 'init_boot')"
    )
    db.execute(
        """
        INSERT INTO stock_firmwares (
            public_token, device_codename, region_code, model_code, version_str,
            android_version, package_type, file_size_str, md5_or_sha256,
            original_official_url, fast_cdn_url, published_date, ed25519_signature, extract_status
        )
        VALUES (?, 'tokay', 'Global', 'tokay', 'AD1A.240905.004', '15.0.0', 'recovery_zip',
                '2.00 GB', ?, ?, ?, '2024-09-01', ?, 'done')
        """,
        (preexisting_token, _sha("tokay-ota-15"), preexisting_url, preexisting_url, preexisting_sig),
    )

    counted_db = CountingD1Wrapper(db)

    try:
        with (
            _make_google_meta_mock_client() as meta_client,
            _make_range_mock_client() as range_client,
        ):
            c_res1 = run_collector(
                "google",
                state_dir=state_dir,
                adapter=GoogleAdapter(),
                metadata_client=meta_client,
                range_client=range_client,
                resolve_dns=False,
                outbox_batch_size=40,
            )
            assert c_res1["status"] == "ok"
            assert c_res1["devices_discovered"] == 3
            assert c_res1["spooled_specs"] == 9

            p_res1 = run_publisher(
                state_dir=state_dir,
                db=counted_db,  # type: ignore[arg-type]
                private_key_hex=priv_key,
                batch_size=40,
                utc_day_fn=lambda: "2026-11-14",
            )
            assert p_res1["status"] == "ok"
            assert p_res1["bootstrapped_rows"] == 1
            assert p_res1["published_specs"] == 9

            # Verify all 9 D1 stock_firmwares rows have valid Ed25519 signatures and preserved preexisting token!
            fw_rows = db.query("SELECT * FROM stock_firmwares ORDER BY id ASC")
            assert len(fw_rows) == 9
            for row in fw_rows:
                payload = build_firmware_payload(row["public_token"], row["fast_cdn_url"])
                assert verify_signature(payload, row["ed25519_signature"], pub_key) is True
                assert row["file_size_str"] in ("2.00 GB", "420.0 MB")

            bootstrapped_row = [
                r
                for r in fw_rows
                if r["device_codename"] == "tokay"
                and r["version_str"] == "AD1A.240905.004"
                and r["package_type"] == "recovery_zip"
            ][0]
            assert bootstrapped_row["public_token"] == preexisting_token
            assert bootstrapped_row["ed25519_signature"] == preexisting_sig
            # Already-completed extraction state ('done') was NOT clobbered when URL didn't change!
            assert bootstrapped_row["extract_status"] == "done"

            # Verify firmware_metadata rows exist for all 9 firmwares
            meta_rows = db.query("SELECT * FROM firmware_metadata")
            assert len(meta_rows) == 9

            # Verify collector DB never created or touched `crawler_sources`
            collector_conn = sqlite3.connect(str(state_dir / "collectors" / "google.sqlite"))
            c_tables = {
                r[0]
                for r in collector_conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            collector_conn.close()
            assert "crawler_sources" not in c_tables

            # Second identical run: BOTH Collector->Publisher AND re-spooled identical Outbox specs
            # MUST execute 0 D1 reads and 0 D1 writes!
            counted_db.reset_counts()
            c_res2 = run_collector(
                "google",
                state_dir=state_dir,
                adapter=GoogleAdapter(),
                metadata_client=meta_client,
                range_client=range_client,
                resolve_dns=False,
            )
            assert c_res2["spooled_specs"] == 0
            p_res2 = run_publisher(
                state_dir=state_dir,
                db=counted_db,  # type: ignore[arg-type]
                private_key_hex=priv_key,
                utc_day_fn=lambda: "2026-11-14",
            )
            assert p_res2["status"] == "ok"
            assert counted_db.query_calls == 0
            assert counted_db.batch_calls == 0

            # Even if identical specs are explicitly re-spooled into outbox/pending/,
            # publisher_ledger.sqlite filters them with 0 D1 reads and 0 D1 writes!
            outbox = OutboxSpool(state_dir)
            done_specs = outbox.read_specs(outbox.list_done()[0])
            outbox.enqueue_specs("google", done_specs)
            counted_db.reset_counts()
            p_res3 = run_publisher(
                state_dir=state_dir,
                db=counted_db,  # type: ignore[arg-type]
                private_key_hex=priv_key,
                utc_day_fn=lambda: "2026-11-14",
            )
            assert p_res3["status"] == "ok"
            assert p_res3["skipped_unchanged"] == 9
            assert counted_db.query_calls == 0
            assert counted_db.batch_calls == 0
    finally:
        db.close()


def test_publisher_quota_7500_mid_stream_checkpoint_and_next_day_resume(tmp_path: Path):
    state_dir = tmp_path / "quota_state"
    d1_sqlite = tmp_path / "quota_d1.sqlite"
    db = _init_migrated_d1(d1_sqlite)
    priv_key, pub_key = generate_keypair()

    try:
        with (
            _make_google_meta_mock_client() as meta_client,
            _make_range_mock_client() as range_client,
        ):
            # Spool all 9 specs into a single outbox file, then publish with batch_size=5 (2 batches: 5 + 4)
            run_collector(
                "google",
                state_dir=state_dir,
                adapter=GoogleAdapter(),
                metadata_client=meta_client,
                range_client=range_client,
                resolve_dns=False,
                outbox_batch_size=40,
            )

        # Fail with D1 error 7500 on batch #2 (after batch #1 of 5 specs commits!)
        quota_db = CountingD1Wrapper(db, fail_on_batch_number=2)
        p_day1 = run_publisher(
            state_dir=state_dir,
            db=quota_db,  # type: ignore[arg-type]
            private_key_hex=priv_key,
            batch_size=5,
            utc_day_fn=lambda: "2026-11-14",
        )
        assert p_day1["status"] == "paused_d1_quota"
        assert p_day1["published_specs"] == 5
        assert p_day1["batches_committed"] == 1
        assert len(db.query("SELECT id FROM stock_firmwares")) == 5

        # Pending file in outbox/pending/ was atomically rewritten to ONLY the 4 uncommitted specs!
        outbox = OutboxSpool(state_dir)
        pending = outbox.list_pending()
        assert len(pending) == 1
        remaining_specs = outbox.read_specs(pending[0])
        assert len(remaining_specs) == 4

        # Same-UTC-day re-run immediately pauses with 0 D1 reads and 0 D1 writes!
        quota_db.fail_on_batch_number = None
        quota_db.reset_counts()
        p_same_day = run_publisher(
            state_dir=state_dir,
            db=quota_db,  # type: ignore[arg-type]
            private_key_hex=priv_key,
            batch_size=5,
            utc_day_fn=lambda: "2026-11-14",
        )
        assert p_same_day["status"] == "paused_d1_quota"
        assert p_same_day["phase"] == "circuit_breaker"
        assert quota_db.query_calls == 0
        assert quota_db.batch_calls == 0

        # Next UTC day ("2026-11-15") automatically unblocks, publishes ONLY the remaining 4 specs, and finishes!
        p_next_day = run_publisher(
            state_dir=state_dir,
            db=quota_db,  # type: ignore[arg-type]
            private_key_hex=priv_key,
            batch_size=5,
            utc_day_fn=lambda: "2026-11-15",
        )
        assert p_next_day["status"] == "ok"
        assert p_next_day["published_specs"] == 4
        assert p_next_day["batches_committed"] == 1
        assert len(outbox.list_pending()) == 0

        all_fw = db.query("SELECT * FROM stock_firmwares")
        assert len(all_fw) == 9
        for row in all_fw:
            assert verify_signature(
                build_firmware_payload(row["public_token"], row["fast_cdn_url"]),
                row["ed25519_signature"],
                pub_key,
            )
    finally:
        db.close()


class _CollisionTestAdapter(VendorAdapter):
    vendor_id = "google"

    def discover_devices(self, http: OfficialMetadataHTTP) -> list[AdapterDevice]:
        return [
            AdapterDevice(
                codename="tokay",
                brand_id="google",
                marketing_name="Google Pixel 9",
                boot_partition_type="init_boot",
            )
        ]

    def fetch_releases(
        self, device: AdapterDevice, http: OfficialMetadataHTTP
    ) -> list[AdapterRelease]:
        # First two share the exact same 5-tuple identity_key but have conflicting URLs & checksums;
        # third release is clean and MUST NOT be blocked!
        return [
            AdapterRelease(
                device_codename="tokay",
                model_code="tokay",
                region_code="Global",
                version_str="AD1A.240905.004",
                android_version="15.0.0",
                package_type="recovery_zip",
                original_official_url="https://dl.google.com/dl/android/aosp/tokay-a.zip",
                fast_cdn_url="https://dl.google.com/dl/android/aosp/tokay-a.zip",
                md5_or_sha256=_sha("collision-a"),
            ),
            AdapterRelease(
                device_codename="tokay",
                model_code="tokay",
                region_code="Global",
                version_str="AD1A.240905.004",
                android_version="15.0.0",
                package_type="recovery_zip",
                original_official_url="https://dl.google.com/dl/android/aosp/tokay-b.zip",
                fast_cdn_url="https://dl.google.com/dl/android/aosp/tokay-b.zip",
                md5_or_sha256=_sha("collision-b"),
            ),
            AdapterRelease(
                device_codename="tokay",
                model_code="tokay",
                region_code="Global",
                version_str="AP3A.241005.015",
                android_version="15.0.0",
                package_type="recovery_zip",
                original_official_url="https://dl.google.com/dl/android/aosp/tokay-clean.zip",
                fast_cdn_url="https://dl.google.com/dl/android/aosp/tokay-clean.zip",
                md5_or_sha256=_sha("clean-release"),
            ),
        ]


def test_5tuple_collision_quarantines_locally_without_blocking_and_locks_work(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    state_dir = tmp_path / "col_state"
    with (
        _make_google_meta_mock_client() as meta_client,
        _make_range_mock_client() as range_client,
    ):
        res = run_collector(
            "google",
            state_dir=state_dir,
            adapter=_CollisionTestAdapter(),
            metadata_client=meta_client,
            range_client=range_client,
            resolve_dns=False,
        )
        assert res["status"] == "ok"
        assert res["collisions_quarantined"] == 1
        assert res["spooled_specs"] == 1

        outbox = OutboxSpool(state_dir)
        spooled = outbox.read_specs(outbox.list_pending()[0])
        assert len(spooled) == 1
        assert spooled[0].release.version_str == "AP3A.241005.015"

    # Non-blocking flock returns skipped_locked immediately when lock is held
    with acquire_file_lock(state_dir / "locks" / "collect-google.lock"):
        locked_res = run_collector("google", state_dir=state_dir, adapter=_CollisionTestAdapter())
        assert locked_res["status"] == "skipped_locked"

    # CLI status subcommand outputs structured JSON
    rc = pipeline_cli_main(["--state-dir", str(state_dir), "status"])
    assert rc == 0
    cli_json = json.loads(capsys.readouterr().out)
    assert cli_json["vendors"]["google"]["quarantined_collisions"] == 1
    assert get_pipeline_status(state_dir=state_dir)["outbox"]["pending_files"] == 1
