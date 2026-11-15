"""Offline unit tests for XiaomiAdapter, OfficialMetadataHTTP.post, verifier VERIFIED_RESOLVER + ETag MD5,
and collector last_checked_at rotation + multi-brand verification.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
import sqlite3
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from fmr_ingest_stock.adapters.base import (
    OFFICIAL_METADATA_HOSTS,
    AdapterDevice,
    AdapterRelease,
    OfficialMetadataHTTP,
    VendorAdapter,
)
from fmr_ingest_stock.adapters.xiaomi import (
    FAST_OSS_HOST,
    MIOTA_V3_URL,
    ULTIMATE_OTA_HOST,
    XiaomiAdapter,
    build_miota_request_payload,
    build_miota_seed_version,
    decrypt_miota_payload,
    encrypt_miota_payload,
    load_xiaomi_device_tags,
)
from fmr_ingest_stock.pipeline import OutboxSpool, RangeProbeVerifier, run_collector


def _md5(label: str) -> str:
    return hashlib.md5(label.encode("utf-8")).hexdigest()


def _sha256(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 1. OfficialMetadataHTTP.post, client property & expanded allowlist
# ---------------------------------------------------------------------------


def test_official_metadata_hosts_client_property_and_post_no_cache_retry(tmp_path: Path) -> None:
    expected_new_hosts = {
        "www.asus.com",
        "odinapi.asus.com",
        "www.vivo.com",
        "component-otapc-cn.allawntech.com",
        "component-otapc-sg.allawnos.com",
        "component-otapc-in.allawnos.com",
        "component-otapc-eu.allawnos.com",
        "component-ota-cn.allawntech.com",
        "component-ota-sg.allawnos.com",
        "component-ota-in.allawnos.com",
        "component-ota-eu.allawnos.com",
    }
    assert expected_new_hosts.issubset(OFFICIAL_METADATA_HOSTS)

    attempts = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal attempts
        assert req.method == "POST"
        attempts += 1
        if attempts == 1:
            return httpx.Response(503, text="Temporary Unavailable")
        if req.url.path == "/too-big":
            return httpx.Response(200, content=b"x" * 2048)
        return httpx.Response(
            200,
            headers={"ETag": '"post-etag"', "Content-Type": "application/json"},
            content=b'{"ok":true}',
        )

    cache_db = tmp_path / "post_cache.sqlite"
    with httpx.Client(transport=httpx.MockTransport(handler)) as raw_client:
        with OfficialMetadataHTTP(
            client=raw_client,
            cache_db_path=cache_db,
            max_bytes=1024,
            max_retries=2,
            sleep_fn=lambda _: None,
        ) as http:
            assert http.client is raw_client

            # Transient 503 retried and succeeds on attempt 2
            res = http.post(
                MIOTA_V3_URL,
                content="q=abc&t=&s=1",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            assert res.status_code == 200
            assert res.from_cache is False
            assert res.text == '{"ok":true}'
            assert attempts == 2

            # POST responses are NEVER written to http_cache
            conn = sqlite3.connect(str(cache_db))
            cached_count = conn.execute("SELECT COUNT(*) FROM http_cache").fetchone()[0]
            conn.close()
            assert cached_count == 0

            # Enforces max_bytes cap on POST body
            with pytest.raises(ValueError, match="exceed"):
                http.post("https://update.miui.com/too-big", json_body={"a": 1})

            # Rejects third-party hosts on POST
            with pytest.raises(ValueError, match="OFFICIAL_METADATA_HOSTS"):
                http.post("https://evil.example.com/api", content=b"123")


# ---------------------------------------------------------------------------
# 2. XiaomiAdapter AES-128-CBC, Seed Versions, Discovery & miotaV3 Protocol
# ---------------------------------------------------------------------------


def test_xiaomi_aes_roundtrip_seed_versions_and_tags_yaml() -> None:
    tags = load_xiaomi_device_tags()
    required_devices = {
        "yingtian": "BM",
        "houji": "NC",
        "shennong": "NB",
        "aurora": "NA",
        "dada": "OC",
        "haotian": "OB",
        "xuanyuan": "OA",
        "vermeer": "NK",
        "manet": "NM",
        "duchamp": "NL",
        "garnet": "NR",
        "zircon": "NO",
        "peridot": "NP",
        "rodin": "OJ",
        "miro": "OK",
        "zorn": "OM",
        "fuxi": "MC",
        "nuwa": "MB",
        "ishtar": "MA",
        "mondrian": "MN",
        "marble": "MR",
        "corot": "ML",
        "aristotle": "MF",
        "rothko": "NN",
        "degas": "NE",
    }
    for codename, expected_tag in required_devices.items():
        assert codename in tags
        assert tags[codename]["tag"] == expected_tag

    assert tags["yingtian"]["android_versions"] == [17]
    assert tags["yingtian"]["regions"] == ["CN"]

    assert build_miota_seed_version(14, "NC", "CNXM") == "V816.0.1.0.UNCCNXM"
    assert build_miota_seed_version(15, "OC", "MIXM") == "OS2.0.1.0.VOCMIXM"
    assert build_miota_seed_version(16, "OC", "EUXM") == "OS3.0.1.0.WOCEUXM"
    assert build_miota_seed_version(17, "BM", "CNXM") == "OS4.0.1.0.XBMCNXM"

    payload = build_miota_request_payload(
        base_code="yingtian",
        region="CN",
        android_major=17,
        tag="BM",
    )
    assert payload["b"] == "F"
    assert payload["f"] == "1"
    assert payload["p"] == "yingtian"
    assert payload["pn"] == "yingtian"
    assert payload["v"] == "MIUI-OS4.0.1.0.XBMCNXM"
    assert payload["options"] == {
        "zone": 1,
        "sv": 3,
        "av": "8.8.0",
        "cv": "OS4.0.1.0.XBMCNXM",
    }

    encrypted = encrypt_miota_payload(payload)
    assert "+" not in encrypted and "/" not in encrypted
    decrypted = decrypt_miota_payload(encrypted)
    assert decrypted == payload

    # Unpadded Base64 responses from miotaV3.php also decrypt cleanly
    assert decrypt_miota_payload(encrypted.rstrip("=")) == payload


def test_xiaomi_adapter_yingtian_dada_sgxm_blockota_rejection_and_oss_urls(tmp_path: Path) -> None:
    mini_tags_yaml = tmp_path / "xiaomi_device_tags.yaml"
    mini_tags_yaml.write_text(
        """
yingtian:
  tag: BM
  marketing_name: Xiaomi Pad 9 Pro Max
  models: [2609BPN09C]
  android_versions: [17]
  regions: [CN]
dada:
  tag: OC
  marketing_name: Xiaomi 15
  models: [24129PN74C]
  android_versions: [15]
  regions: [CN, Global, SG]
fuxi:
  tag: MC
  marketing_name: Xiaomi 13
  models: [2211133C]
  android_versions: [13]
  regions: [CN]
""",
        encoding="utf-8",
    )

    play_csv = (
        "Retail Branding,Marketing Name,Device,Model\n"
        "Xiaomi,Xiaomi 15,dada_global,24129PN74G\n"
        "Redmi,Redmi Note 99 Untracked,untracked_dev,25999RN99G\n"
        "POCO,POCO Watch,pocowatch,M2131W1\n"
    ).encode("utf-16")

    yingtian_md5 = _md5("yingtian-os4")
    dada_cn_md5 = _md5("dada-cn-os2")
    dada_gl_md5 = _md5("dada-global-os2")
    dada_sg_md5 = _md5("dada-sg-os2")
    fuxi_miui_md5 = _md5("fuxi-miui14")

    observed_miota_payloads: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "storage.googleapis.com" and req.url.path == "/play_public/supported_devices.csv":
            return httpx.Response(200, content=play_csv)

        if str(req.url) == MIOTA_V3_URL and req.method == "POST":
            body_text = req.content.decode("utf-8")
            qs = parse_qs(body_text, keep_blank_values=True)
            assert qs.get("t") == [""]
            assert qs.get("s") == ["1"]
            q_val = qs["q"][0]
            req_json = decrypt_miota_payload(q_val)
            observed_miota_payloads.append(req_json)

            pn = req_json["pn"]
            cv = req_json["options"]["cv"]

            if pn == "yingtian" and cv == "OS4.0.1.0.XBMCNXM":
                resp_obj = {
                    "LatestRom": {
                        "version": "OS4.0.16.0.XBMCNXM",
                        "codebase": "17.0",
                        "filename": "yingtian-ota_full-OS4.0.16.0.XBMCNXM-user-17.0-aabbccdd11.zip?t=1763000000&s=signedtoken",
                        "md5": yingtian_md5.upper(),
                        "filesize": "7516192768",
                    },
                    "CurrentRom": {
                        # Incremental blockota MUST be rejected!
                        "version": "OS4.0.16.0.XBMCNXM",
                        "codebase": "17.0",
                        "filename": "miui-blockota-yingtian-OS4.0.1.0.XBMCNXM-OS4.0.16.0.XBMCNXM-1122334455-17.0.zip?t=1&s=2",
                        "md5": _md5("blockota-bad"),
                        "filesize": "419430400",
                    },
                }
                return httpx.Response(200, text=encrypt_miota_payload(resp_obj))

            if pn == "dada" and cv == "OS2.0.1.0.VOCCNXM":
                assert req_json["p"] == "dada"
                assert req_json["options"]["zone"] == 1
                resp_obj = {
                    "LatestRom": {
                        "version": "OS2.0.105.0.VOCCNXM",
                        "codebase": "15.0",
                        "filename": "dada-ota_full-OS2.0.105.0.VOCCNXM-user-15.0-1234567890.zip?t=999&s=sig",
                        "md5": dada_cn_md5,
                        "filesize": 6979321856,
                    },
                    "CurrentRom": {
                        # Bad non-32-hex MD5 MUST be rejected!
                        "version": "OS2.0.100.0.VOCCNXM",
                        "codebase": "15.0",
                        "filename": "dada-ota_full-OS2.0.100.0.VOCCNXM-user-15.0-0000000000.zip",
                        "md5": "not-a-valid-md5",
                        "filesize": 6900000000,
                    },
                }
                return httpx.Response(200, text=encrypt_miota_payload(resp_obj))

            if pn == "dada_global" and cv == "OS2.0.1.0.VOCMIXM":
                assert req_json["p"] == "dada"
                assert req_json["options"]["zone"] == 2
                resp_obj = {
                    "CurrentRom": {
                        "version": "OS2.0.8.0.VOCMIXM",
                        "codebase": "15.0",
                        "filename": "dada_global-ota_full-OS2.0.8.0.VOCMIXM-user-15.0-fedcba9876.zip?t=123&s=xyz",
                        "md5": dada_gl_md5,
                        "filesize": "6442450944",
                    }
                }
                return httpx.Response(200, text=encrypt_miota_payload(resp_obj))

            if pn == "dada_sg_global" and cv == "OS2.0.1.0.VOCSGXM":
                resp_obj = {
                    "LatestRom": {
                        "version": "OS2.0.3.0.VOCSGXM",
                        "codebase": "15.0",
                        "filename": "dada_sg_global-ota_full-OS2.0.3.0.VOCSGXM-user-15.0-5566778899.zip",
                        "md5": dada_sg_md5,
                        "filesize": "6442450944",
                    }
                }
                return httpx.Response(200, text=encrypt_miota_payload(resp_obj))

            if pn == "fuxi" and cv == "V14.0.1.0.TMCCNXM":
                resp_obj = {
                    "LatestRom": {
                        "version": "V14.0.28.0.TMCCNXM",
                        "codebase": "13.0",
                        "filename": "miui_FUXI_V14.0.28.0.TMCCNXM_abcdef1234_13.0.zip",
                        "md5": fuxi_miui_md5,
                        "filesize": "5905580032",
                    }
                }
                return httpx.Response(200, text=encrypt_miota_payload(resp_obj))

            return httpx.Response(200, text=encrypt_miota_payload({"CurrentRom": {}, "LatestRom": {}}))

        raise AssertionError(f"Unexpected request: {req.method} {req.url}")

    adapter = XiaomiAdapter(device_tags_path=mini_tags_yaml)
    with httpx.Client(transport=httpx.MockTransport(handler)) as raw_client:
        with OfficialMetadataHTTP(client=raw_client) as http:
            devices = {d.codename: d for d in adapter.discover_devices(http)}
            # Merged CN-only yingtian + YAML/Play dada + YAML fuxi (excluding untracked Play-only rows & POCO Watch)
            assert set(devices.keys()) == {"dada", "fuxi", "yingtian"}
            assert "untracked_dev" not in devices
            assert devices["yingtian"].vendor_meta["tag"] == "BM"
            assert set(devices["dada"].model_numbers) == {"24129PN74C", "24129PN74G"}

            # 1. yingtian Pad 9 Pro Max -> OS4.0.16.0.XBMCNXM, Android 17, HyperOS, tokenless OSS URL
            yt_rels = adapter.fetch_releases(devices["yingtian"], http)
            assert len(yt_rels) == 1
            yt = yt_rels[0]
            assert yt.version_str == "OS4.0.16.0.XBMCNXM"
            assert yt.android_version == "17"
            assert yt.region_code == "CN"
            assert yt.os_name == "HyperOS"
            assert yt.md5_or_sha256 == yingtian_md5
            assert "?" not in yt.fast_cdn_url
            assert (
                yt.fast_cdn_url
                == f"https://{FAST_OSS_HOST}/OS4.0.16.0.XBMCNXM/yingtian-ota_full-OS4.0.16.0.XBMCNXM-user-17.0-aabbccdd11.zip"
            )
            assert (
                yt.original_official_url
                == f"https://{ULTIMATE_OTA_HOST}/OS4.0.16.0.XBMCNXM/yingtian-ota_full-OS4.0.16.0.XBMCNXM-user-17.0-aabbccdd11.zip"
            )
            assert yt.to_publish_spec(devices["yingtian"]).release.version_str == "OS4.0.16.0.XBMCNXM"

            # 2. dada CN, dada_global MIXM -> Global, and SGXM -> SG
            dada_rels = {r.region_code: r for r in adapter.fetch_releases(devices["dada"], http)}
            assert set(dada_rels.keys()) == {"CN", "Global", "SG"}
            assert dada_rels["CN"].version_str == "OS2.0.105.0.VOCCNXM"
            assert dada_rels["CN"].model_code == "dada"
            assert dada_rels["Global"].version_str == "OS2.0.8.0.VOCMIXM"
            assert dada_rels["Global"].model_code == "dada_global"
            assert dada_rels["SG"].version_str == "OS2.0.3.0.VOCSGXM"
            assert dada_rels["SG"].region_code == "SG"

            # Direct `dada_global` AdapterDevice also works cleanly
            direct_dada_global = AdapterDevice(
                codename="dada_global",
                brand_id="xiaomi",
                marketing_name="Xiaomi 15",
                vendor_meta={"tag": "OC", "android_versions": [15]},
            )
            direct_gl_rels = adapter.fetch_releases(direct_dada_global, http)
            assert len(direct_gl_rels) == 1
            assert direct_gl_rels[0].region_code == "Global"
            assert direct_gl_rels[0].version_str == "OS2.0.8.0.VOCMIXM"

            # 3. Legacy V14.* release sets os_name="MIUI"
            fuxi_rels = adapter.fetch_releases(devices["fuxi"], http)
            assert len(fuxi_rels) == 1
            assert fuxi_rels[0].os_name == "MIUI"

            # 4. Untracked Play device without known 2-letter tag returns [] without error
            untracked_device = AdapterDevice(
                codename="untracked_dev",
                brand_id="xiaomi",
                marketing_name="Redmi Note 99 Untracked",
            )
            assert adapter.fetch_releases(untracked_device, http) == []


# ---------------------------------------------------------------------------
# 3. Verifier VERIFIED_RESOLVER Pass-Through & ETag MD5 Capture
# ---------------------------------------------------------------------------


def test_verifier_verified_resolver_passthrough_and_etag_md5_capture() -> None:
    zip_head = b"PK\x03\x04" + (b"\x00" * 60)
    vivo_etag_md5 = "ABCDEF0123456789ABCDEF0123456789"
    range_calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        range_calls.append(str(req.url))
        if req.url.host == "sysupwksdl.vivoglobal.com":
            return httpx.Response(
                206,
                headers={
                    "Content-Type": "application/zip",
                    "Content-Range": f"bytes 0-63/{5 * 1024 * 1024 * 1024}",
                    "ETag": f'W/"{vivo_etag_md5}"',
                },
                content=zip_head,
            )
        raise AssertionError(f"Unexpected HTTP Range call to {req.url}")

    valid_refresh_url = f"https://findmyrom.com/stock/refresh/{_sha256('oplus-1')}"
    valid_bundle_url = f"https://findmyrom.com/sony/bundles/{_sha256('sony-1')}"

    resolver_rel = AdapterRelease(
        device_codename="oneplus-13",
        model_code="CPH2653",
        region_code="EU",
        version_str="CPH2653_15.0.0.401",
        android_version="15",
        package_type="recovery_zip",
        original_official_url=valid_refresh_url,
        fast_cdn_url=valid_refresh_url,
        delivery_mode="resolver_oplus",
        verification_status="VERIFIED_RESOLVER",
        resolver_meta={"source": {"vendor": "oneplus", "model": "CPH2653"}},
        file_size_bytes=6442450944,
        file_size_str="6.00 GB",
    )

    bundle_rel = AdapterRelease(
        device_codename="XQ-EC54",
        model_code="XQ-EC54",
        region_code="EEA",
        version_str="69.0.A.2.38",
        android_version="14",
        package_type="recovery_zip",
        original_official_url=valid_bundle_url,
        fast_cdn_url=valid_bundle_url,
        delivery_mode="r2_bundle",
        verification_status="VERIFIED_R2_BUNDLE",
        resolver_meta={"bundle_sha256": _sha256("sony-1")},
        file_size_bytes=5368709120,
        file_size_str="5.00 GB",
    )

    bad_resolver_no_meta = AdapterRelease(
        device_codename="oneplus-13",
        model_code="CPH2653",
        region_code="EU",
        version_str="CPH2653_15.0.0.402",
        android_version="15",
        package_type="recovery_zip",
        original_official_url=valid_refresh_url,
        fast_cdn_url=valid_refresh_url,
        delivery_mode="resolver_oplus",
        verification_status="VERIFIED_RESOLVER",
        resolver_meta=None,
    )

    bad_resolver_url = AdapterRelease(
        device_codename="oneplus-13",
        model_code="CPH2653",
        region_code="EU",
        version_str="CPH2653_15.0.0.403",
        android_version="15",
        package_type="recovery_zip",
        original_official_url="https://findmyrom.com/stock/refresh/not-64-hex",
        fast_cdn_url="https://findmyrom.com/stock/refresh/not-64-hex",
        delivery_mode="resolver_oplus",
        verification_status="VERIFIED_RESOLVER",
        resolver_meta={"source": {"vendor": "oneplus"}},
    )

    vivo_rel = AdapterRelease(
        device_codename="vivo-x100-pro",
        model_code="PD2337F",
        region_code="IN",
        version_str="PD2337F_EX_A_14.0.7.10",
        android_version="14",
        package_type="recovery_zip",
        original_official_url="https://sysupwksdl.vivoglobal.com/upgrade/official/PD2337F-full.zip",
        fast_cdn_url="https://sysupwksdl.vivoglobal.com/upgrade/official/PD2337F-full.zip",
        md5_or_sha256=None,
    )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with RangeProbeVerifier(client=client, resolve_dns=False) as verifier:
            ok_res = verifier.verify_one("oneplus", resolver_rel)
            assert ok_res.status == "VERIFIED"
            assert ok_res.http_status == 200
            assert ok_res.final_url == valid_refresh_url
            assert range_calls == []  # ZERO HTTP calls to findmyrom.com!

            ok_bundle = verifier.verify_one("sony", bundle_rel)
            assert ok_bundle.status == "VERIFIED"
            assert ok_bundle.http_status == 200
            assert range_calls == []

            # with_probe_result also accepts valid resolver URLs without rejecting findmyrom.com
            hydrated = resolver_rel.with_probe_result(brand_id="oneplus")
            assert hydrated.fast_cdn_url == valid_refresh_url

            assert verifier.verify_one("oneplus", bad_resolver_no_meta).status == "DEAD_UPSTREAM"
            assert verifier.verify_one("oneplus", bad_resolver_url).status == "DEAD_UPSTREAM"
            assert range_calls == []

            # Direct CDN Range response captures 32-hex ETag MD5 in lowercase
            ok_vivo = verifier.verify_one("vivo", vivo_rel)
            assert ok_vivo.status == "VERIFIED"
            assert ok_vivo.release.md5_or_sha256 == vivo_etag_md5.lower()
            assert len(range_calls) == 1


# ---------------------------------------------------------------------------
# 4. Collector `last_checked_at` Rotation, DB Upgrade & Multi-Brand (`asus`/`rog`)
# ---------------------------------------------------------------------------


class _MultiBrandRotationAdapter(VendorAdapter):
    vendor_id = "asus"

    def __init__(self, polled_log: list[str]) -> None:
        self.polled_log = polled_log

    def discover_devices(self, http: OfficialMetadataHTTP) -> list[AdapterDevice]:
        return [
            AdapterDevice(
                codename="rog-phone-9",
                brand_id="rog",
                marketing_name="ROG Phone 9",
            ),
            AdapterDevice(
                codename="zenfone-11-ultra",
                brand_id="asus",
                marketing_name="ASUS Zenfone 11 Ultra",
            ),
            AdapterDevice(
                codename="zenfone-10",
                brand_id="asus",
                marketing_name="ASUS Zenfone 10",
            ),
        ]

    def fetch_releases(
        self, device: AdapterDevice, http: OfficialMetadataHTTP
    ) -> list[AdapterRelease]:
        self.polled_log.append(device.codename)
        url = f"https://dlcdnets.asus.com/pub/ASUS/{device.codename}-user.zip"
        return [
            AdapterRelease(
                device_codename=device.codename,
                model_code=device.codename.upper(),
                region_code="WW",
                version_str="35.0210.0210.100",
                android_version="15",
                package_type="recovery_zip",
                original_official_url=url,
                fast_cdn_url=url,
                md5_or_sha256=None,
            )
        ]


def test_collector_db_migration_last_checked_at_rotation_and_multibrand_verify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = tmp_path / "state"
    db_path = state_dir / "collectors" / "asus.sqlite"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # Pre-create legacy collector_devices table WITHOUT last_checked_at to verify seamless migration!
    legacy_conn = sqlite3.connect(str(db_path))
    legacy_conn.executescript(
        """
        CREATE TABLE collector_devices (
            codename TEXT PRIMARY KEY,
            brand_id TEXT NOT NULL,
            marketing_name TEXT NOT NULL,
            model_numbers_json TEXT NOT NULL,
            boot_partition_type TEXT NOT NULL,
            device_sha256 TEXT NOT NULL,
            updated_at INTEGER NOT NULL
        );
        INSERT INTO collector_devices VALUES (
            'rog-phone-9', 'rog', 'ROG Phone 9', '[]', 'init_boot', 'abc', 100
        );
        """
    )
    legacy_conn.commit()
    legacy_conn.close()

    zip_head = b"PK\x03\x04" + (b"\x00" * 60)

    def range_handler(req: httpx.Request) -> httpx.Response:
        etag = _md5(str(req.url))
        return httpx.Response(
            206,
            headers={
                "Content-Type": "application/zip",
                "Content-Range": f"bytes 0-63/{3 * 1024 * 1024 * 1024}",
                "ETag": f'"{etag}"',
            },
            content=zip_head,
        )

    # Simulate deadline expiring after 2 devices in run #1 so 3rd device rotates to front in run #2!
    fake_mono = [1000.0]

    def fake_monotonic() -> float:
        return fake_mono[0]

    monkeypatch.setattr("fmr_ingest_stock.pipeline.collector.time.monotonic", fake_monotonic)

    polled_run1: list[str] = []

    class _SteppingAdapter(_MultiBrandRotationAdapter):
        def fetch_releases(
            self, device: AdapterDevice, http: OfficialMetadataHTTP
        ) -> list[AdapterRelease]:
            rels = super().fetch_releases(device, http)
            if len(self.polled_log) >= 2:
                # Advance monotonic past deadline before idx=2 is polled
                fake_mono[0] += 100.0
            return rels

    now_counter = [1_700_000_000]

    def next_clock() -> int:
        now_counter[0] += 10
        return now_counter[0]

    with (
        httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(404))) as meta_client,
        httpx.Client(transport=httpx.MockTransport(range_handler)) as range_client,
    ):
        res1 = run_collector(
            "asus",
            state_dir=state_dir,
            adapter=_SteppingAdapter(polled_run1),
            metadata_client=meta_client,
            range_client=range_client,
            resolve_dns=False,
            max_collect_seconds=10,
            now_fn=next_clock,
        )
        assert res1["status"] == "ok"
        # Sorted initially by (0, codename): rog-phone-9, zenfone-10; zenfone-11-ultra deferred by deadline!
        assert polled_run1 == ["rog-phone-9", "zenfone-10"]
        assert res1["probed_verified"] == 2
        assert res1["spooled_specs"] == 2

        # Run #2: zenfone-11-ultra has last_checked_at=0 so it MUST be polled FIRST!
        polled_run2: list[str] = []
        res2 = run_collector(
            "asus",
            state_dir=state_dir,
            adapter=_MultiBrandRotationAdapter(polled_run2),
            metadata_client=meta_client,
            range_client=range_client,
            resolve_dns=False,
            max_collect_seconds=600,
            now_fn=next_clock,
        )
        assert res2["status"] == "ok"
        assert polled_run2 == ["zenfone-11-ultra", "rog-phone-9", "zenfone-10"]
        # Only the newly verified zenfone-11-ultra is spooled; cached ETag MD5s for the first 2 are preserved!
        assert res2["probed_verified"] == 1
        assert res2["skipped_cached_probe"] == 2
        assert res2["spooled_specs"] == 1

    outbox = OutboxSpool(state_dir)
    all_specs = [
        spec
        for p in outbox.list_pending()
        for spec in outbox.read_specs(p)
    ]
    assert len(all_specs) == 3
    brands_spooled = {s.device.codename: s.device.brand_id for s in all_specs}
    assert brands_spooled == {
        "rog-phone-9": "rog",
        "zenfone-10": "asus",
        "zenfone-11-ultra": "asus",
    }
    # Every spooled spec captured its 32-hex ETag MD5!
    assert all(s.release.md5_or_sha256 and len(s.release.md5_or_sha256) == 32 for s in all_specs)
