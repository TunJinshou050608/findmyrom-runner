"""Offline unit tests for OPlus /update/v3 PC-channel OTA client, OPlusAdapter, and vendor registry."""

from __future__ import annotations

import base64
from collections import Counter
import json
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
import httpx
import pytest

from fmr_ingest_stock import download_refresh
from fmr_ingest_stock.adapters import (
    OfficialMetadataHTTP,
    VendorAdapter,
    get_adapter,
    load_vendor_configs,
)
from fmr_ingest_stock.adapters.oplus import (
    OPlusAdapter,
    select_regions_for_model,
)
from fmr_ingest_stock.oplus_ota import (
    OPLUS_USER_ID,
    REGION_CONFIGS,
    OPlusOTAClient,
    clean_oplus_cdn_url,
    zero_tail_seed_version,
)


@pytest.fixture()
def patched_oplus_rsa_keys(monkeypatch: pytest.MonkeyPatch) -> rsa.RSAPrivateKey:
    """Install an ephemeral 2048-bit RSA keypair across REGION_CONFIGS for full crypto roundtrips."""
    priv_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub_pem = (
        priv_key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.PKCS1,
        )
        .decode("ascii")
    )
    for region_code, cfg in REGION_CONFIGS.items():
        patched = dict(cfg)
        patched["pubkey_pem"] = pub_pem
        monkeypatch.setitem(REGION_CONFIGS, region_code, patched)
    return priv_key


def _server_decrypt_request(
    request: httpx.Request,
    priv_key: rsa.RSAPrivateKey,
) -> tuple[bytes, dict[str, Any], dict[str, Any]]:
    protected_hdr = json.loads(request.headers["protectedKey"])
    scene1 = protected_hdr["SCENE_1"]
    enc_key_bytes = base64.b64decode(scene1["protectedKey"])
    b64_aes_key = priv_key.decrypt(
        enc_key_bytes,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA1()),
            algorithm=hashes.SHA1(),
            label=None,
        ),
    )
    aes_key = base64.b64decode(b64_aes_key)
    envelope_in = json.loads(request.content.decode("utf-8"))
    params = json.loads(envelope_in["params"])
    iv = base64.b64decode(params["iv"])
    cipher_bytes = base64.b64decode(params["cipher"])
    decryptor = Cipher(algorithms.AES(aes_key), modes.CTR(iv)).decryptor()
    plain = json.loads((decryptor.update(cipher_bytes) + decryptor.finalize()).decode("utf-8"))
    return aes_key, scene1, plain


def _server_encrypt_response(aes_key: bytes, body_obj: dict[str, Any]) -> dict[str, Any]:
    resp_iv = b"S" * 16
    encryptor = Cipher(algorithms.AES(aes_key), modes.CTR(resp_iv)).encryptor()
    raw = json.dumps(body_obj, separators=(",", ":")).encode("utf-8")
    cipher_b64 = base64.b64encode(encryptor.update(raw) + encryptor.finalize()).decode("ascii")
    return {
        "responseCode": 200,
        "body": json.dumps(
            {"cipher": cipher_b64, "iv": base64.b64encode(resp_iv).decode("ascii")},
            separators=(",", ":"),
        ),
    }


def test_zero_tail_seed_and_clean_oplus_cdn_url() -> None:
    assert zero_tail_seed_version("PJD110", "A") == "PJD110_11.A.01_0001_000000000001"
    assert zero_tail_seed_version("CPH2765", "c") == "CPH2765_11.C.01_0001_000000000001"

    dirty_cn_url = (
        "https://gauss-compotacostauto-cn.allawnfs.com"
        "/remove-72a948c78b314fb6a5cb2f93938291ab"
        "/component-ota/26/02/10/PJD110_11.C.62_full.zip"
    )
    cleaned = clean_oplus_cdn_url(dirty_cn_url)
    assert cleaned == (
        "https://gauss-compotacostauto-cn.allawnfs.com"
        "/component-ota/26/02/10/PJD110_11.C.62_full.zip"
    )

    # Non-allawnfs URLs remain untouched
    other = "https://component-ota-sg.allawnos.com/downloadCheck?c=abc123"
    assert clean_oplus_cdn_url(other) == other


def test_select_regions_for_model_prefixes() -> None:
    assert select_regions_for_model("CPH2765") == ("SG", "EU", "IN")
    assert select_regions_for_model("RMX5011") == ("SG", "IN", "EU", "CN")
    assert select_regions_for_model("PJD110") == ("CN",)
    assert select_regions_for_model("PHZ110") == ("CN",)
    assert select_regions_for_model("PKB110") == ("CN",)
    assert select_regions_for_model("NE2210") == ("CN", "SG", "EU", "IN")
    assert select_regions_for_model("OPD2403") == ("CN", "SG", "EU", "IN")


def test_v3_pc_roundtrip_retry_and_packet_filtering(
    patched_oplus_rsa_keys: rsa.RSAPrivateKey,
) -> None:
    attempts = {"count": 0}
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://component-otapc-cn.allawntech.com/update/v3"
        assert request.headers.get("channel") == "pc"
        assert request.headers.get("nvCarrier") == "10010111"

        aes_key, scene1, plain = _server_decrypt_request(request, patched_oplus_rsa_keys)
        assert scene1["negotiationVersion"] == "1615879139745"
        assert plain["type"] == "1"

        attempts["count"] += 1
        if attempts["count"] == 1:
            return httpx.Response(503, text="Service Unavailable")

        ota_ver = request.headers["otaVersion"]
        if ".A." in ota_ver:
            # Incremental / Delta OTA (type == "2") -> must NOT be full OTA
            return httpx.Response(
                200,
                json=_server_encrypt_response(
                    aes_key,
                    {
                        "otaVersion": "PJD110_11.A.20_0200_202601011200",
                        "versionName": "PJD110_15.0.0.200(CN01)",
                        "components": [
                            {
                                "componentPackets": {
                                    "type": "2",
                                    "size": "950000000",
                                    "url": "https://gauss-compotacostauto-cn.allawnfs.com/delta.zip",
                                }
                            }
                        ],
                    },
                ),
            )
        if ".C." in ota_ver:
            # GKA attestation required (gkaReq == 1) -> must NOT be full OTA
            return httpx.Response(
                200,
                json=_server_encrypt_response(
                    aes_key,
                    {
                        "otaVersion": "PJD110_11.C.30_0300_202602011200",
                        "versionName": "PJD110_15.0.0.300(CN01)",
                        "gkaReq": 1,
                        "components": [
                            {
                                "componentPackets": {
                                    "type": "1",
                                    "size": "6800000000",
                                    "url": "https://component-ota-cn.allawntech.com/download?c=gka",
                                }
                            }
                        ],
                    },
                ),
            )
        return httpx.Response(
            200,
            json=_server_encrypt_response(
                aes_key,
                {
                    "otaVersion": "PJD110_11.F.62_0620_202603101800",
                    "versionName": "PJD110_16.0.0.620(CN01)",
                    "androidVersion": "Android 16",
                    "colorOSVersion": "ColorOS 16.0",
                    "securityPatch": "2026-03-01",
                    "gkaReq": 0,
                    "components": [
                        {
                            "componentPackets": {
                                "type": "1",
                                "size": "7123456789",
                                "md5": "aabbccddeeff00112233445566778899",
                                "url": (
                                    "https://gauss-compotacostauto-cn.allawnfs.com"
                                    "/remove-0123456789abcdef0123456789abcdef"
                                    "/component-ota/26/03/10/PJD110_16.0.0.620.zip"
                                ),
                            }
                        }
                    ],
                },
            ),
        )

    mock_client = httpx.Client(transport=httpx.MockTransport(handler))
    with OPlusOTAClient(http_client=mock_client, max_retries=2, sleep_fn=sleeps.append) as ota:
        delta_pkt = ota.query_update("PJD110", zero_tail_seed_version("PJD110", "A"), region="CN")
        assert attempts["count"] == 2
        assert len(sleeps) == 1
        assert delta_pkt.packet_type == "2"
        assert delta_pkt.is_full_ota is False

        gka_pkt = ota.query_update("PJD110", zero_tail_seed_version("PJD110", "C"), region="CN")
        assert gka_pkt.gka_req == 1
        assert gka_pkt.is_full_ota is False

        full_pkt = ota.query_update("PJD110", zero_tail_seed_version("PJD110", "F"), region="CN")
        assert full_pkt.packet_type == "1"
        assert full_pkt.gka_req == 0
        assert full_pkt.is_full_ota is True
        assert full_pkt.best_url == (
            "https://gauss-compotacostauto-cn.allawnfs.com"
            "/component-ota/26/03/10/PJD110_16.0.0.620.zip"
        )


def test_oneplus_12_cn_pjd110_direct_cdn_and_delta_rejection(
    patched_oplus_rsa_keys: rsa.RSAPrivateKey,
) -> None:
    csv_bytes = (
        "Retail Branding,Marketing Name,Device,Model\n"
        "OnePlus,OnePlus 12,OP5929L1,PJD110\n"
        "OnePlus,OnePlus Watch 2,OPWWE231,OPWWE231\n"
    ).encode("utf-16")

    seen_pc_requests: list[tuple[str, str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "storage.googleapis.com":
            return httpx.Response(200, content=csv_bytes)

        if request.url.host == "component-otapc-cn.allawntech.com" and request.url.path == "/update/v3":
            assert request.headers.get("channel") == "pc"
            model_hdr = request.headers["model"]
            ota_hdr = request.headers["otaVersion"]
            seen_pc_requests.append((model_hdr, ota_hdr, request.headers["nvCarrier"]))

            aes_key, _, _ = _server_decrypt_request(request, patched_oplus_rsa_keys)
            if "_11.A." in ota_hdr:
                # Delta OTA must be skipped
                return httpx.Response(
                    200,
                    json=_server_encrypt_response(
                        aes_key,
                        {
                            "otaVersion": "PJD110_11.A.15_0150_202512010000",
                            "versionName": "PJD110_14.0.0.150(CN01)",
                            "components": [
                                {
                                    "componentPackets": {
                                        "type": "2",
                                        "size": "850000000",
                                        "url": "https://gauss-compotacostauto-cn.allawnfs.com/delta.zip",
                                    }
                                }
                            ],
                        },
                    ),
                )
            if "_11.C." in ota_hdr:
                # GKA packet must be skipped
                return httpx.Response(
                    200,
                    json=_server_encrypt_response(
                        aes_key,
                        {
                            "otaVersion": "PJD110_11.C.40_0400_202601100000",
                            "versionName": "PJD110_15.0.0.400(CN01)",
                            "gkaReq": 1,
                            "components": [
                                {
                                    "componentPackets": {
                                        "type": "1",
                                        "size": "6900000000",
                                        "url": "https://gauss-compotacostauto-cn.allawnfs.com/gka.zip",
                                    }
                                }
                            ],
                        },
                    ),
                )
            if "_11.F." in ota_hdr:
                return httpx.Response(
                    200,
                    json=_server_encrypt_response(
                        aes_key,
                        {
                            "otaVersion": "PJD110_11.F.82_0820_202604121530",
                            "versionName": "PJD110_16.0.0.820(CN01)",
                            "androidVersion": "Android 16",
                            "colorOSVersion": "ColorOS 16.0",
                            "securityPatch": "2026-04-01",
                            "gkaReq": 0,
                            "components": [
                                {
                                    "componentPackets": {
                                        "type": "1",
                                        "size": "7340032000",
                                        "md5": "0123456789abcdef0123456789abcdef",
                                        "url": (
                                            "https://gauss-compotacostauto-cn.allawnfs.com"
                                            "/remove-fedcba9876543210fedcba9876543210"
                                            "/component-ota/26/04/12/PJD110_16.0.0.820_CN01.zip"
                                        ),
                                    }
                                }
                            ],
                        },
                    ),
                )
            return httpx.Response(200, json={"responseCode": 2004, "body": None})

        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport, follow_redirects=False) as raw_client:
        http = OfficialMetadataHTTP(client=raw_client, sleep_fn=lambda _: None)
        adapter = OPlusAdapter("oneplus")
        devices = adapter.discover_devices(http)
        assert len(devices) == 1
        dev = devices[0]
        assert dev.brand_id == "oneplus"
        assert dev.model_numbers == ("PJD110",)

        releases = adapter.fetch_releases(dev, http)

    assert len(seen_pc_requests) == 4  # CN only * 1 carrier * 4 branches (A, C, F, H)
    assert seen_pc_requests[0] == ("PJD110", "PJD110_11.A.01_0001_000000000001", "10010111")
    assert len(releases) == 1

    rel = releases[0]
    expected_clean_url = (
        "https://gauss-compotacostauto-cn.allawnfs.com"
        "/component-ota/26/04/12/PJD110_16.0.0.820_CN01.zip"
    )
    assert rel.model_code == "PJD110"
    assert rel.region_code == "CN"
    assert rel.package_type == "recovery_zip"
    assert rel.delivery_mode == "direct_cdn"
    assert rel.verification_status == "VERIFIED_IMMUTABLE"
    assert rel.original_official_url == expected_clean_url
    assert rel.fast_cdn_url == expected_clean_url
    assert rel.resolver_meta is None
    assert rel.md5_or_sha256 == "0123456789abcdef0123456789abcdef"
    assert rel.file_size_bytes == 7340032000
    assert rel.security_patch == "2026-04-01"
    assert rel.published_date == "2026-04-12"
    assert rel.os_name == "OxygenOS"

    # Must produce a valid PublishSpecV1
    spec = rel.to_publish_spec(dev)
    assert spec.release.original_official_url == expected_clean_url


def test_oppo_find_n6_global_cph2765_download_check_refresh(
    patched_oplus_rsa_keys: rsa.RSAPrivateKey,
) -> None:
    csv_bytes = (
        "Retail Branding,Marketing Name,Device,Model\n"
        "OPPO,OPPO Find N6,OP6125L1,CPH2765\n"
    ).encode("utf-8")

    gate_url = "https://component-ota-sg.allawnos.com/downloadCheck?c=eyJ0b2tlbiI6ImNwaDI3NjUifQ=="
    signed_cdn_url = (
        "https://gauss-compotacostauto-sg.allawnfs.com"
        "/component-ota/26/05/02/CPH2765export_11_16.0.0.510EX01_20260502.zip"
        "?Expires=1790000000&Signature=xyz"
    )
    gate_methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "storage.googleapis.com":
            return httpx.Response(200, content=csv_bytes)

        if request.url.host == "component-otapc-sg.allawnos.com" and request.url.path == "/update/v3":
            assert request.headers.get("channel") == "pc"
            aes_key, scene1, _ = _server_decrypt_request(request, patched_oplus_rsa_keys)
            assert scene1["negotiationVersion"] == "1615895993238"

            if (
                request.headers["nvCarrier"] == "00101100"
                and "_11.A." in request.headers["otaVersion"]
            ):
                return httpx.Response(
                    200,
                    json=_server_encrypt_response(
                        aes_key,
                        {
                            "otaVersion": "CPH2765_11.A.51_0510_202605021020",
                            "versionName": "CPH2765_16.0.0.510(EX01)",
                            "androidVersion": "16",
                            "colorOSVersion": "ColorOS 16.0",
                            "securityPatch": "2026-05-05",
                            "gkaReq": 0,
                            "components": [
                                {
                                    "componentPackets": {
                                        "type": "1",
                                        "size": "6912345678",
                                        "md5": "53e8751e9ce8ad0f1a1fafa289ee53ac",
                                        "manualUrl": gate_url,
                                        "url": gate_url,
                                    }
                                }
                            ],
                        },
                    ),
                )
            return httpx.Response(200, json={"responseCode": 2004, "body": None})

        if request.url.host in (
            "component-otapc-eu.allawnos.com",
            "component-otapc-in.allawnos.com",
        ):
            return httpx.Response(200, json={"responseCode": 2004, "body": None})

        if request.url.host == "component-ota-sg.allawnos.com" and request.url.path == "/downloadCheck":
            gate_methods.append(request.method)
            assert request.headers.get("userId") == OPLUS_USER_ID
            return httpx.Response(302, headers={"Location": signed_cdn_url})

        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport, follow_redirects=False) as raw_client:
        http = OfficialMetadataHTTP(client=raw_client, sleep_fn=lambda _: None)
        adapter = OPlusAdapter("oppo")
        devices = adapter.discover_devices(http)
        assert len(devices) == 1
        dev = devices[0]
        assert dev.brand_id == "oppo"
        assert dev.model_numbers == ("CPH2765",)

        releases = adapter.fetch_releases(dev, http)

    assert gate_methods == ["GET"]
    assert len(releases) == 1
    rel = releases[0]

    expected_source = {
        "schema": 1,
        "vendor": "oppo",
        "version": "CPH2765_16.0.0.510(EX01)",
        "gate_url": gate_url,
        "filename": "CPH2765export_11_16.0.0.510EX01_20260502.zip",
    }
    expected_hash = download_refresh.fingerprint(expected_source)
    expected_refresh_url = f"https://findmyrom.com/stock/refresh/{expected_hash}"

    assert rel.package_type == "recovery_zip"
    assert rel.delivery_mode == "resolver_oplus"
    assert rel.verification_status == "VERIFIED_RESOLVER"
    assert rel.original_official_url == expected_refresh_url
    assert rel.fast_cdn_url == expected_refresh_url
    assert rel.resolver_meta == {"source": expected_source}
    assert rel.md5_or_sha256 == "53e8751e9ce8ad0f1a1fafa289ee53ac"
    assert rel.file_size_bytes == 6912345678
    assert rel.security_patch == "2026-05-05"
    assert rel.os_name == "ColorOS"

    spec = rel.to_publish_spec(dev)
    assert spec.release.original_official_url == expected_refresh_url


def test_vendor_configs_8_active_official_staggered_and_get_adapter() -> None:
    configs = load_vendor_configs()
    enabled_vendors = {vid: cfg for vid, cfg in configs.items() if cfg.enabled}

    expected_active = {
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
    }
    assert set(enabled_vendors.keys()) == expected_active

    bucket_counts: Counter[int] = Counter()
    for vid, cfg in enabled_vendors.items():
        assert cfg.status == "active_official", vid
        assert cfg.bucket is not None and 0 <= cfg.bucket <= 11, vid
        bucket_counts[cfg.bucket] += 1

    assert max(bucket_counts.values()) <= 2

    # Check disabled / covered / suppressed vendor statuses
    assert configs["rog"].enabled is False
    assert configs["rog"].status == "covered_by_asus_adapter"
    assert configs["sony"].enabled is False
    assert configs["sony"].status == "awaiting_r2_and_cdf_seed"
    assert configs["motorola"].enabled is False
    assert configs["motorola"].status == "requires_lmsa_oauth_token"
    for checkin_vid in ("nothing",):
        assert configs[checkin_vid].enabled is False
        assert configs[checkin_vid].status == "requires_device_authenticated_checkin"
    for suppressed_vid in ("huawei", "honor"):
        assert configs[suppressed_vid].enabled is False
        assert configs[suppressed_vid].status == "suppressed_closed_bootloader"

    # Instantiate every registered official vendor adapter via get_adapter()
    for registered_vid in (
        "google",
        "samsung",
        "asus",
        "rog",
        "vivo",
        "oneplus",
        "oppo",
        "realme",
        "tecno",
        "infinix",
        "itel",
    ):
        inst = get_adapter(registered_vid)
        assert isinstance(inst, VendorAdapter)

    import importlib.util
    import sys
    import types

    if importlib.util.find_spec("fmr_ingest_stock.adapters.xiaomi") is not None:
        xiaomi_inst = get_adapter("xiaomi")
        assert isinstance(xiaomi_inst, VendorAdapter)
        assert xiaomi_inst.vendor_id == "xiaomi"
    else:
        fake_mod = types.ModuleType("fmr_ingest_stock.adapters.xiaomi")

        class _StubXiaomiAdapter(VendorAdapter):
            vendor_id = "xiaomi"

            def discover_devices(self, http: OfficialMetadataHTTP) -> list[Any]:
                return []

            def fetch_releases(self, device: Any, http: OfficialMetadataHTTP) -> list[Any]:
                return []

        setattr(fake_mod, "XiaomiAdapter", _StubXiaomiAdapter)
        sys.modules["fmr_ingest_stock.adapters.xiaomi"] = fake_mod
        try:
            xiaomi_inst = get_adapter("xiaomi")
            assert isinstance(xiaomi_inst, _StubXiaomiAdapter)
            assert xiaomi_inst.vendor_id == "xiaomi"
        finally:
            sys.modules.pop("fmr_ingest_stock.adapters.xiaomi", None)

    assert get_adapter("oneplus").vendor_id == "oneplus"
    assert get_adapter("oppo").vendor_id == "oppo"
    assert get_adapter("realme").vendor_id == "realme"
    assert get_adapter("rog").vendor_id == "asus"


def test_realme_gt_7_pro_rmx5011_os_name_and_discovery(
    patched_oplus_rsa_keys: rsa.RSAPrivateKey,
) -> None:
    csv_bytes = (
        "Retail Branding,Marketing Name,Device,Model\n"
        "realme,realme GT 7 Pro,RE5C69,RMX5011\n"
    ).encode("utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "storage.googleapis.com":
            return httpx.Response(200, content=csv_bytes)
        if request.url.host == "component-otapc-in.allawnos.com" and "_11.A." in request.headers["otaVersion"]:
            aes_key, _, _ = _server_decrypt_request(request, patched_oplus_rsa_keys)
            return httpx.Response(
                200,
                json=_server_encrypt_response(
                    aes_key,
                    {
                        "otaVersion": "RMX5011_11.A.28_0280_202603201200",
                        "versionName": "RMX5011_15.0.0.280(EX01)",
                        "androidVersion": "15",
                        "colorOSVersion": "V15.0",
                        "securityPatch": "2026-03-05",
                        "gkaReq": 0,
                        "components": [
                            {
                                "componentPackets": {
                                    "type": "1",
                                    "size": "6500000000",
                                    "md5": "abcdefabcdefabcdefabcdefabcdefab",
                                    "url": "https://gauss-compotacostauto-in.allawnfs.com/ota/RMX5011_15.0.0.280.zip",
                                }
                            }
                        ],
                    },
                ),
            )
        return httpx.Response(200, json={"responseCode": 2004, "body": None})

    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False) as raw_client:
        http = OfficialMetadataHTTP(client=raw_client, sleep_fn=lambda _: None)
        adapter = OPlusAdapter("realme")
        devices = adapter.discover_devices(http)
        assert len(devices) == 1
        releases = adapter.fetch_releases(devices[0], http)

    assert len(releases) == 1
    assert releases[0].os_name == "realme UI"
    assert releases[0].region_code == "IN"
    assert releases[0].delivery_mode == "direct_cdn"
    assert releases[0].verification_status == "VERIFIED_IMMUTABLE"
