"""Offline unit tests for v4 OEM adapter dark bugs uncovered during 88-device live stress test."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from fmr_ingest_stock.adapters.asus import AsusAdapter
from fmr_ingest_stock.adapters.base import (
    OFFICIAL_METADATA_HOSTS,
    AdapterDevice,
    OfficialMetadataHTTP,
)
from fmr_ingest_stock.adapters.oplus import (
    OPLUS_OTA_MODEL_RE,
    OPlusAdapter,
    normalize_oplus_model,
    select_regions_for_model,
)
from fmr_ingest_stock.adapters.samsung import SamsungAdapter
from fmr_ingest_stock.adapters.vivo import (
    DEFAULT_VIVO_REGIONS,
    VIVO_PORTAL_LOCALE_PATHS,
    VivoAdapter,
)
from fmr_ingest_stock.adapters.xiaomi import (
    MIOTA_V3_URL,
    XiaomiAdapter,
    build_miota_seed_version,
    decrypt_miota_payload,
    encrypt_miota_payload,
    load_xiaomi_device_tags,
)
from fmr_ingest_stock.samsung_fus import (
    FUSAuthError,
    FirmwareUnavailable,
    MAX_FUS_BINARY_BYTES,
    SmartFUS,
    normalize_version,
)


# ---------------------------------------------------------------------------
# 1. Samsung SmartFUS Status S01/Sxx/Fxx, 401 Retry, Version/Size Guards
# ---------------------------------------------------------------------------


def test_smartfus_status_classification_version_and_size_bounds() -> None:
    # Empty segments must fail normalize_version
    for bad_ver in ("A//B/C", "A/B/C/", "/B/C/D", "///", "A//C"):
        with pytest.raises(ValueError):
            normalize_version(bad_ver)

    # Valid 3-part and Wi-Fi empty-PHONE 4-part normalize cleanly
    assert normalize_version("S928BXXS4AXK1/S928BOXM4AXK1/S928BXXS4AXK1") == (
        "S928BXXS4AXK1/S928BOXM4AXK1/S928BXXS4AXK1/S928BXXS4AXK1"
    )
    assert normalize_version("X910XXU1AWGH/X910OXM1AWGH//X910XXU1AWGH") == (
        "X910XXU1AWGH/X910OXM1AWGH/X910XXU1AWGH/X910XXU1AWGH"
    )

    # Unavailable status codes -> FirmwareUnavailable; 401 -> FUSAuthError; 500 -> plain ValueError
    unavailable_codes = ("S01", "S02", "S99", "F00", "F12", "F99", "400", "403", "404", "410")
    for code in unavailable_codes:
        def unavail_handler(req: httpx.Request, status_code: str = code) -> httpx.Response:
            if req.url.path.endswith("GenerateNonce.do"):
                return httpx.Response(200, headers={"nonce": "abcdefghijklmnop"})
            return httpx.Response(
                200,
                headers={"Content-Type": "application/xml"},
                text=f"<FUSMsg><FUSBody><Results><Status>{status_code}</Status></Results></FUSBody></FUSMsg>",
            )

        with httpx.Client(transport=httpx.MockTransport(unavail_handler)) as client:
            with pytest.raises(FirmwareUnavailable, match=code):
                SmartFUS(client).binary_info("SM-S928B", "EUX", "PDA/CSC/MODEM")

    def auth_401_handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("GenerateNonce.do"):
            return httpx.Response(200, headers={"nonce": "abcdefghijklmnop"})
        return httpx.Response(
            200,
            headers={"Content-Type": "application/xml"},
            text="<FUSMsg><FUSBody><Results><Status>401</Status></Results></FUSBody></FUSMsg>",
        )

    with httpx.Client(transport=httpx.MockTransport(auth_401_handler)) as client:
        with pytest.raises(FUSAuthError, match="401"):
            SmartFUS(client).binary_info("SM-S928B", "EUX", "PDA/CSC/MODEM")

    def server_500_handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("GenerateNonce.do"):
            return httpx.Response(200, headers={"nonce": "abcdefghijklmnop"})
        return httpx.Response(
            200,
            headers={"Content-Type": "application/xml"},
            text="<FUSMsg><FUSBody><Results><Status>500</Status></Results></FUSBody></FUSMsg>",
        )

    with httpx.Client(transport=httpx.MockTransport(server_500_handler)) as client:
        with pytest.raises(ValueError) as exc_info:
            SmartFUS(client).binary_info("SM-S928B", "EUX", "PDA/CSC/MODEM")
        assert not isinstance(exc_info.value, (FirmwareUnavailable, FUSAuthError))

    # Size bounds: 0 and >64 GiB rejected; exact 64 GiB accepted
    for bad_size in (0, -100, MAX_FUS_BINARY_BYTES + 1):
        def bad_size_handler(req: httpx.Request, sz: int = bad_size) -> httpx.Response:
            if req.url.path.endswith("GenerateNonce.do"):
                return httpx.Response(200, headers={"nonce": "abcdefghijklmnop"})
            return httpx.Response(
                200,
                headers={"Content-Type": "application/xml"},
                text=f"""<FUSMsg><FUSBody><Results><Status>S00</Status></Results><Put>
                <BINARY_SW_VERSION><Data>PDA/CSC/MODEM/PDA</Data></BINARY_SW_VERSION>
                <BINARY_NAME><Data>SM-S928B_pkg.zip.enc4</Data></BINARY_NAME>
                <MODEL_PATH><Data>/SmartSwitch/SM-S928B/</Data></MODEL_PATH>
                <BINARY_BYTE_SIZE><Data>{sz}</Data></BINARY_BYTE_SIZE>
                </Put></FUSBody></FUSMsg>""",
            )

        with httpx.Client(transport=httpx.MockTransport(bad_size_handler)) as client:
            with pytest.raises(ValueError, match="size"):
                SmartFUS(client).binary_info("SM-S928B", "EUX", "PDA/CSC/MODEM")


def test_samsung_adapter_default_zero_upgrades_s01_survival_and_401_retry() -> None:
    default_adapter = SamsungAdapter()
    assert default_adapter.max_upgrades_per_csc == 0

    eux_xml = """<?xml version="1.0" encoding="UTF-8"?>
    <versioninfo><firmware><model>SM-S928B</model><cc>EUX</cc><version>
      <latest o="15">S928BXXS4AXK1/S928BOXM4AXK1/S928BXXS4AXK1</latest>
      <upgrade>
        <value>S928BXXU1AWM9/S928BOXM1AWM9/S928BXXU1AWM9</value>
      </upgrade>
    </version></firmware></versioninfo>
    """
    ins_xml = """<?xml version="1.0" encoding="UTF-8"?>
    <versioninfo><firmware><model>SM-S928B</model><cc>INS</cc><version>
      <latest o="15">S928BXXS4AXK5/S928BOWO4AXK5/S928BXXS4AXK5</latest>
    </version></firmware></versioninfo>
    """

    inform_calls: list[str] = []
    ins_attempts = {"count": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        host = req.url.host
        path = req.url.path
        if host == "fota-cloud-dn.ospserver.net":
            if path == "/firmware/EUX/SM-S928B/version.xml":
                return httpx.Response(200, headers={"Content-Type": "application/xml"}, text=eux_xml)
            if path == "/firmware/INS/SM-S928B/version.xml":
                return httpx.Response(200, headers={"Content-Type": "application/xml"}, text=ins_xml)
            return httpx.Response(404)
        if host == "doc.samsungmobile.com":
            return httpx.Response(404)
        if host == "neofussvr.sslcs.cdngc.net":
            if path == "/NF_SmartDownloadGenerateNonce.do":
                return httpx.Response(
                    200,
                    headers={
                        "nonce": "abcdefghijklmnop",
                        "Set-Cookie": "SESSION=fus_sess_1; Domain=.cdngc.net; Path=/",
                    },
                )
            body_str = req.content.decode("utf-8")
            inform_calls.append(body_str)
            # Historical upgrade build returns S01 -> must NOT abort device!
            if "S928BXXU1AWM9" in body_str:
                return httpx.Response(
                    200,
                    headers={"Content-Type": "application/xml"},
                    text="<FUSMsg><FUSBody><Results><Status>S01</Status></Results></FUSBody></FUSMsg>",
                )
            # EUX latest succeeds cleanly
            if "S928BXXS4AXK1" in body_str:
                return httpx.Response(
                    200,
                    headers={"Content-Type": "application/xml"},
                    text="""<FUSMsg><FUSBody><Results><Status>S00</Status></Results><Put>
                    <BINARY_SW_VERSION><Data>S928BXXS4AXK1/S928BOXM4AXK1/S928BXXS4AXK1/S928BXXS4AXK1</Data></BINARY_SW_VERSION>
                    <BINARY_NAME><Data>SM-S928B_1_20261101120000_eux_fac.zip.enc4</Data></BINARY_NAME>
                    <MODEL_PATH><Data>/SmartSwitch/SM-S928B/</Data></MODEL_PATH>
                    <BINARY_BYTE_SIZE><Data>8589934592</Data></BINARY_BYTE_SIZE>
                    </Put></FUSBody></FUSMsg>""",
                )
            # INS latest returns 401 on first attempt, then succeeds on fresh-session retry!
            if "S928BXXS4AXK5" in body_str:
                ins_attempts["count"] += 1
                if ins_attempts["count"] == 1:
                    return httpx.Response(
                        200,
                        headers={"Content-Type": "application/xml"},
                        text="<FUSMsg><FUSBody><Results><Status>401</Status></Results></FUSBody></FUSMsg>",
                    )
                return httpx.Response(
                    200,
                    headers={"Content-Type": "application/xml"},
                    text="""<FUSMsg><FUSBody><Results><Status>200</Status></Results><Put>
                    <BINARY_SW_VERSION><Data>S928BXXS4AXK5/S928BOWO4AXK5/S928BXXS4AXK5/S928BXXS4AXK5</Data></BINARY_SW_VERSION>
                    <BINARY_NAME><Data>SM-S928B_1_20261105120000_ins_fac.zip.enc4</Data></BINARY_NAME>
                    <MODEL_PATH><Data>/SmartSwitch/SM-S928B/</Data></MODEL_PATH>
                    <BINARY_BYTE_SIZE><Data>8589934592</Data></BINARY_BYTE_SIZE>
                    </Put></FUSBody></FUSMsg>""",
                )
        raise AssertionError(f"Unexpected URL: {req.url}")

    dev = AdapterDevice(
        codename="samsung-e3q",
        brand_id="samsung",
        marketing_name="Samsung Galaxy S24 Ultra",
        model_numbers=("SM-S928B",),
        vendor_meta={"models": ["SM-S928B"]},
    )

    # 1. Default max_upgrades_per_csc=0 never queries S928BXXU1AWM9 upgrade
    with httpx.Client(transport=httpx.MockTransport(handler)) as raw_client:
        raw_client.cookies.set("SESSION", "stale_cookie", domain="neofussvr.sslcs.cdngc.net")
        with OfficialMetadataHTTP(client=raw_client, allowed_hosts=OFFICIAL_METADATA_HOSTS) as http:
            rels_default = SamsungAdapter(cscs=("EUX", "INS")).fetch_releases(dev, http)
    assert len(rels_default) == 2
    assert not any("S928BXXU1AWM9" in c for c in inform_calls)
    assert ins_attempts["count"] == 2

    # 2. Explicit max_upgrades_per_csc=2 queries S928BXXU1AWM9, absorbs S01, and preserves both latest releases
    inform_calls.clear()
    ins_attempts["count"] = 0
    with httpx.Client(transport=httpx.MockTransport(handler)) as raw_client:
        with OfficialMetadataHTTP(client=raw_client, allowed_hosts=OFFICIAL_METADATA_HOSTS) as http:
            rels_with_upg = SamsungAdapter(
                cscs=("EUX", "INS"),
                max_upgrades_per_csc=2,
            ).fetch_releases(dev, http)
    assert any("S928BXXU1AWM9" in c for c in inform_calls)
    assert {r.region_code for r in rels_with_upg} == {"EUX", "INS"}

    # 3. Transient httpx.ConnectError (e.g. TLS EOF on SmartFUS keep-alive) retries once and survives
    tls_eof_state = {"eux_nonce_calls": 0}

    def tls_eof_handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "fota-cloud-dn.ospserver.net":
            if req.url.path == "/firmware/EUX/SM-S928B/version.xml":
                return httpx.Response(200, headers={"Content-Type": "application/xml"}, text=eux_xml)
            return httpx.Response(404)
        if req.url.host == "doc.samsungmobile.com":
            return httpx.Response(404)
        if req.url.host == "neofussvr.sslcs.cdngc.net":
            if req.url.path == "/NF_SmartDownloadGenerateNonce.do":
                tls_eof_state["eux_nonce_calls"] += 1
                if tls_eof_state["eux_nonce_calls"] == 1:
                    raise httpx.ConnectError("[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred")
                return httpx.Response(200, headers={"nonce": "abcdefghijklmnop"})
            return httpx.Response(
                200,
                headers={"Content-Type": "application/xml"},
                text="""<FUSMsg><FUSBody><Results><Status>S00</Status></Results><Put>
                <BINARY_SW_VERSION><Data>S928BXXS4AXK1/S928BOXM4AXK1/S928BXXS4AXK1/S928BXXS4AXK1</Data></BINARY_SW_VERSION>
                <BINARY_NAME><Data>SM-S928B_1_20261101120000_eux_fac.zip.enc4</Data></BINARY_NAME>
                <MODEL_PATH><Data>/SmartSwitch/SM-S928B/</Data></MODEL_PATH>
                <BINARY_BYTE_SIZE><Data>8589934592</Data></BINARY_BYTE_SIZE>
                </Put></FUSBody></FUSMsg>""",
            )
        raise AssertionError(f"Unexpected URL: {req.url}")

    with httpx.Client(transport=httpx.MockTransport(tls_eof_handler)) as raw_client:
        with OfficialMetadataHTTP(client=raw_client, allowed_hosts=OFFICIAL_METADATA_HOSTS) as http:
            rels_eof = SamsungAdapter(cscs=("EUX",)).fetch_releases(dev, http)
    assert len(rels_eof) == 1
    assert tls_eof_state["eux_nonce_calls"] == 2


def test_samsung_fus_502_transient_retry_with_fresh_nonce_and_per_csc_isolation() -> None:
    sleeps: list[float] = []
    state = {"nonce_seq": 0, "inform_calls": 0, "seen_cookie_headers": []}
    nonces = ("AAAAAAAAAAAAAAAA", "BBBBBBBBBBBBBBBB", "CCCCCCCCCCCCCCCC")
    norm_ver = "S928BXXS4AXK1/S928BOXM4AXK1/S928BXXS4AXK1/S928BXXS4AXK1"

    def expected_logic(nonce: str, ver: str) -> str:
        return "".join(ver[ord(c) & 15] if (ord(c) & 15) < len(ver) else "." for c in nonce)

    def transient_502_handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("GenerateNonce.do"):
            state["seen_cookie_headers"].append(req.headers.get("cookie") or "")
            idx = state["nonce_seq"]
            state["nonce_seq"] += 1
            nonce = nonces[min(idx, len(nonces) - 1)]
            return httpx.Response(
                200,
                headers={
                    "nonce": nonce,
                    "Set-Cookie": f"SESSION=sess_{idx}; Domain=.cdngc.net; Path=/",
                },
            )
        state["inform_calls"] += 1
        if state["inform_calls"] == 1:
            return httpx.Response(502, text="<html>502 Bad Gateway</html>")
        if state["inform_calls"] == 2:
            return httpx.Response(503, text="<html>503 Service Unavailable</html>")
        body_xml = req.content.decode("utf-8")
        assert f"<Data>{expected_logic(nonces[2], norm_ver)}</Data>" in body_xml
        return httpx.Response(
            200,
            headers={"Content-Type": "application/xml"},
            text=f"""<FUSMsg><FUSBody><Results><Status>S00</Status></Results><Put>
            <BINARY_SW_VERSION><Data>{norm_ver}</Data></BINARY_SW_VERSION>
            <BINARY_NAME><Data>SM-S928B_1_20261101120000_eux_fac.zip.enc4</Data></BINARY_NAME>
            <MODEL_PATH><Data>/SmartSwitch/SM-S928B/</Data></MODEL_PATH>
            <BINARY_BYTE_SIZE><Data>8589934592</Data></BINARY_BYTE_SIZE>
            </Put></FUSBody></FUSMsg>""",
        )

    with httpx.Client(transport=httpx.MockTransport(transient_502_handler)) as client:
        info = SmartFUS(client, sleep_fn=sleeps.append).binary_info(
            "SM-S928B", "EUX", "S928BXXS4AXK1/S928BOXM4AXK1/S928BXXS4AXK1"
        )
    assert info["version"] == norm_ver
    assert state["nonce_seq"] == 3
    assert state["inform_calls"] == 3
    # Every GenerateNonce.do attempt started with cleared cookies (never leaking consumed session)
    assert state["seen_cookie_headers"] == ["", "", ""]
    assert sleeps == [pytest.approx(0.4), pytest.approx(0.8)]

    # Persistent 502 exhausts 2 retries (3 total attempts) and raises HTTPStatusError from SmartFUS
    always_502_calls = {"nonce": 0, "inform": 0}
    always_502_sleeps: list[float] = []

    def always_502_handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("GenerateNonce.do"):
            always_502_calls["nonce"] += 1
            return httpx.Response(200, headers={"nonce": "DDDDDDDDDDDDDDDD"})
        always_502_calls["inform"] += 1
        return httpx.Response(502, text="Bad Gateway")

    with httpx.Client(transport=httpx.MockTransport(always_502_handler)) as client:
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            SmartFUS(client, sleep_fn=always_502_sleeps.append).binary_info(
                "SM-S928B", "XAA", norm_ver
            )
        assert exc_info.value.response.status_code == 502
    assert always_502_calls == {"nonce": 3, "inform": 3}
    assert always_502_sleeps == [pytest.approx(0.4), pytest.approx(0.8)]

    # Per-CSC survival in SamsungAdapter: EUX recovers from one 502, XAA persistently 502s, INS succeeds
    csc_xml = {
        "EUX": "S928BXXS4AXK1/S928BOXM4AXK1/S928BXXS4AXK1",
        "XAA": "S928BXXS4AXK2/S928BOXM4AXK2/S928BXXS4AXK2",
        "INS": "S928BXXS4AXK3/S928BOWO4AXK3/S928BXXS4AXK3",
    }
    eux_attempts = {"inform": 0}
    xaa_attempts = {"inform": 0}

    def adapter_handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "fota-cloud-dn.ospserver.net":
            for csc_code, ver_str in csc_xml.items():
                if req.url.path == f"/firmware/{csc_code}/SM-S928B/version.xml":
                    return httpx.Response(
                        200,
                        headers={"Content-Type": "application/xml"},
                        text=(
                            f'<?xml version="1.0" encoding="UTF-8"?>'
                            f"<versioninfo><firmware><model>SM-S928B</model><cc>{csc_code}</cc>"
                            f'<version><latest o="15">{ver_str}</latest></version>'
                            f"</firmware></versioninfo>"
                        ),
                    )
            return httpx.Response(404)
        if req.url.host == "doc.samsungmobile.com":
            return httpx.Response(404)
        if req.url.host == "neofussvr.sslcs.cdngc.net":
            if req.url.path == "/NF_SmartDownloadGenerateNonce.do":
                return httpx.Response(200, headers={"nonce": "EEEEEEEEEEEEEEEE"})
            body = req.content.decode("utf-8")
            if "<Data>XAA</Data>" in body:
                xaa_attempts["inform"] += 1
                return httpx.Response(502, text="Bad Gateway")
            if "<Data>EUX</Data>" in body:
                eux_attempts["inform"] += 1
                if eux_attempts["inform"] == 1:
                    return httpx.Response(502, text="Transient Bad Gateway")
                full_v = normalize_version(csc_xml["EUX"])
                return httpx.Response(
                    200,
                    headers={"Content-Type": "application/xml"},
                    text=f"""<FUSMsg><FUSBody><Results><Status>S00</Status></Results><Put>
                    <BINARY_SW_VERSION><Data>{full_v}</Data></BINARY_SW_VERSION>
                    <BINARY_NAME><Data>SM-S928B_1_20261101120000_eux_fac.zip.enc4</Data></BINARY_NAME>
                    <MODEL_PATH><Data>/SmartSwitch/SM-S928B/</Data></MODEL_PATH>
                    <BINARY_BYTE_SIZE><Data>8589934592</Data></BINARY_BYTE_SIZE>
                    </Put></FUSBody></FUSMsg>""",
                )
            if "<Data>INS</Data>" in body:
                full_v = normalize_version(csc_xml["INS"])
                return httpx.Response(
                    200,
                    headers={"Content-Type": "application/xml"},
                    text=f"""<FUSMsg><FUSBody><Results><Status>S00</Status></Results><Put>
                    <BINARY_SW_VERSION><Data>{full_v}</Data></BINARY_SW_VERSION>
                    <BINARY_NAME><Data>SM-S928B_1_20261105120000_ins_fac.zip.enc4</Data></BINARY_NAME>
                    <MODEL_PATH><Data>/SmartSwitch/SM-S928B/</Data></MODEL_PATH>
                    <BINARY_BYTE_SIZE><Data>8589934592</Data></BINARY_BYTE_SIZE>
                    </Put></FUSBody></FUSMsg>""",
                )
        raise AssertionError(f"Unexpected URL: {req.url}")

    dev = AdapterDevice(
        codename="samsung-e3q",
        brand_id="samsung",
        marketing_name="Samsung Galaxy S24 Ultra",
        model_numbers=("SM-S928B",),
        vendor_meta={"models": ["SM-S928B"]},
    )
    with httpx.Client(transport=httpx.MockTransport(adapter_handler)) as raw_client:
        with OfficialMetadataHTTP(client=raw_client, allowed_hosts=OFFICIAL_METADATA_HOSTS) as http:
            rels = SamsungAdapter(cscs=("EUX", "XAA", "INS")).fetch_releases(dev, http)

    assert [r.region_code for r in rels] == ["EUX", "INS"]
    assert eux_attempts["inform"] == 2
    assert xaa_attempts["inform"] == 3


# ---------------------------------------------------------------------------
# 2. Vivo Multi-Zip Camera LUT Filter, " New" Badge Strip & Scoped Metadata
# ---------------------------------------------------------------------------


def test_vivo_lut_zip_filter_badge_strip_and_multi_zip_container_scoping() -> None:
    # Page 1: Real firmware zip + Camera LUT zip + " New" badge on #funtouchName
    page_lut_html = """<!DOCTYPE html>
    <html><body>
      <div class="pkg-box">
        <span id="funtouchName">PD2309F_EX_A_14.0.15.2.W20.V000L1 New</span>
        <p>Release date: 2026-10-20</p>
        <p>File size: 5.60 GB</p>
        <p>MD5: aabbccddeeff00112233445566778899</p>
        <p>System: Funtouch OS 14 (Android 14)</p>
        <a href="https://sysupwksdl.vivoglobal.com/upgrade/official/PD2309F_EX_A_14.0.15.2.W20.V000L1-update-full_1729000000.zip">Download Firmware</a>
        <a href="https://asia-exstatic-vivofs.vivo.com/ps/vivo-Log_ACES_Profiles%20(1).zip">Download LUT</a>
        <a href="https://asia-exstatic-vivofs.vivo.com/ps/vivo_USB_Driver_v2.zip">USB Driver</a>
      </div>
    </body></html>
    """

    # Page 2: Two distinct firmware zips in separate cards -> scoped metadata per card
    page_two_cards_html = """<!DOCTYPE html>
    <html><body>
      <div class="cards-wrapper">
        <div class="fw-card">
          <h4>PD2337F_EX_A_15.0.2.1.W30.V000L1 Hot</h4>
          <p>Release date: 2026-11-01</p>
          <p>File size: 6.90 GB</p>
          <p>MD5: 11111111111111111111111111111111</p>
          <a href="https://sysupwksdl.vivoglobal.com/upgrade/official/PD2337F_EX_A_15.0.2.1.W30.V000L1-update-full.zip">Download A15</a>
        </div>
        <div class="fw-card">
          <h4>PD2337F_EX_A_14.0.9.5.W30.V000L1 Latest</h4>
          <p>Release date: 2026-09-10</p>
          <p>File size: 6.20 GB</p>
          <p>MD5: 22222222222222222222222222222222</p>
          <a href="https://sysupwksdl.vivoglobal.com/upgrade/official/PD2337F_EX_A_14.0.9.5.W30.V000L1-update-full.zip">Download A14</a>
        </div>
      </div>
    </body></html>
    """

    # Page 3: Two distinct firmware zips inside one shared flat div -> ambiguous size/hash must NOT be stamped!
    page_ambiguous_html = """<!DOCTYPE html>
    <html><body>
      <div class="flat-shared">
        <span id="funtouchName">PD2405F_EX_A_15.0.1.0.W30 New</span>
        <p>Release date: 2026-11-02</p>
        <p>File size: 6.40 GB</p>
        <p>MD5: 33333333333333333333333333333333</p>
        <a href="https://sysupwksdl.vivoglobal.com/upgrade/official/PD2405F_EX_A_15.0.1.0.W30-update-full.zip">Download Main</a>
        <a href="https://sysupwksdl.vivoglobal.com/upgrade/official/PD2405F_EX_A_14.0.8.0.W30-update-full.zip">Download Rollback</a>
      </div>
    </body></html>
    """

    def handler(req: httpx.Request) -> httpx.Response:
        mid = req.url.params.get("id")
        if mid == "301":
            return httpx.Response(200, headers={"Content-Type": "text/html"}, text=page_lut_html)
        if mid == "302":
            return httpx.Response(200, headers={"Content-Type": "text/html"}, text=page_two_cards_html)
        if mid == "303":
            return httpx.Response(200, headers={"Content-Type": "text/html"}, text=page_ambiguous_html)
        return httpx.Response(404)

    adapter = VivoAdapter(regions=("en",))
    with httpx.Client(transport=httpx.MockTransport(handler)) as raw_client:
        with OfficialMetadataHTTP(client=raw_client, allowed_hosts=OFFICIAL_METADATA_HOSTS) as http:
            dev_lut = AdapterDevice(
                codename="vivo-x90-pro",
                brand_id="vivo",
                marketing_name="vivo X90 Pro",
                vendor_meta={"region_model_ids": {"en": "301"}},
            )
            rels_lut = adapter.fetch_releases(dev_lut, http)
            assert len(rels_lut) == 1
            assert rels_lut[0].version_str == "PD2309F_EX_A_14.0.15.2.W20.V000L1"
            assert not rels_lut[0].version_str.endswith("_New")
            assert "Log_ACES" not in rels_lut[0].original_official_url
            assert rels_lut[0].md5_or_sha256 == "aabbccddeeff00112233445566778899"
            assert rels_lut[0].file_size_bytes is not None and rels_lut[0].file_size_bytes > 5_000_000_000

            dev_cards = AdapterDevice(
                codename="vivo-x100-pro",
                brand_id="vivo",
                marketing_name="vivo X100 Pro",
                vendor_meta={"region_model_ids": {"en": "302"}},
            )
            rels_cards = {r.version_str: r for r in adapter.fetch_releases(dev_cards, http)}
            assert len(rels_cards) == 2
            r15 = rels_cards["PD2337F_EX_A_15.0.2.1.W30.V000L1"]
            r14 = rels_cards["PD2337F_EX_A_14.0.9.5.W30.V000L1"]
            assert r15.md5_or_sha256 == "11111111111111111111111111111111"
            assert r14.md5_or_sha256 == "22222222222222222222222222222222"
            assert r15.published_date == "2026-11-01"
            assert r14.published_date == "2026-09-10"
            assert r15.file_size_str == "6.90 GB"
            assert r14.file_size_str == "6.20 GB"

            dev_ambig = AdapterDevice(
                codename="vivo-x200-pro",
                brand_id="vivo",
                marketing_name="vivo X200 Pro",
                vendor_meta={"region_model_ids": {"en": "303"}},
            )
            rels_ambig = adapter.fetch_releases(dev_ambig, http)
            assert len(rels_ambig) == 2
            for r in rels_ambig:
                assert r.file_size_bytes is None
                assert r.md5_or_sha256 is None


# ---------------------------------------------------------------------------
# 3. OPlus Nord & realme Pad Model Filter Expansion
# ---------------------------------------------------------------------------


def test_oplus_nord_and_realme_pad_model_filter_and_region_routing() -> None:
    expected_accepted = (
        "AC2001",
        "AC2003",
        "BE2011",
        "BE2029",
        "DE2117",
        "DE2118",
        "DN2101",
        "DN2103",
        "EB2101",
        "EB2103",
        "IV2201",
        "GN2200",
        "MT2110",
        "MT2111",
        "GM1917",
        "HD1905",
        "IN2025",
        "KB2005",
        "LE2125",
        "NE2215",
        "RMP2102",
        "RMP2103",
        "RMP2402",
        "RMP2502",
    )
    for model in expected_accepted:
        assert OPLUS_OTA_MODEL_RE.fullmatch(model), model
        assert normalize_oplus_model(model.lower()) == model

    # Non-phone/non-tablet accessories remain rejected
    for rejected in ("OPWWE231", "RMA2105", "BUDS001", ""):
        assert normalize_oplus_model(rejected) is None

    # International routing for Nord & realme Pad
    assert select_regions_for_model("AC2001") == ("SG", "EU", "IN")
    assert select_regions_for_model("DN2103") == ("SG", "EU", "IN")
    assert select_regions_for_model("RMP2102") == ("SG", "IN", "EU")

    play_csv = (
        "Retail Branding,Marketing Name,Device,Model\n"
        "OnePlus,OnePlus Nord,avicii,AC2001\n"
        "OnePlus,OnePlus Nord,avicii,AC2003\n"
        "OnePlus,OnePlus Nord 2 5G,denniz,DN2103\n"
        "realme,realme Pad,RE58B8,RMP2102\n"
    ).encode("utf-8")

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "storage.googleapis.com":
            return httpx.Response(200, content=play_csv)
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(handler)) as raw_client:
        with OfficialMetadataHTTP(client=raw_client, allowed_hosts=OFFICIAL_METADATA_HOSTS) as http:
            op_devs = OPlusAdapter("oneplus").discover_devices(http)
            rm_devs = OPlusAdapter("realme").discover_devices(http)

    op_models = {m for d in op_devs for m in d.model_numbers}
    rm_models = {m for d in rm_devs for m in d.model_numbers}
    assert {"AC2001", "AC2003", "DN2103"}.issubset(op_models)
    assert "RMP2102" in rm_models


# ---------------------------------------------------------------------------
# 4. ASUS Nested ProductLevel Series Discovery (Zenfone 8..11 + ROG Phone 5..9)
# ---------------------------------------------------------------------------


def test_asus_nested_product_level_discovers_historical_zenfone_and_rog_generations() -> None:
    # Mirrors live ASUS GetPDLevel(type=1): Result.Product is [] while series live in Result.ProductLevel.Products.Items!
    series_payload = {
        "Result": {
            "ProductLevel": {
                "Products": {
                    "Items": [
                        {"Id": "33240", "Name": "Gaming Phone"},
                        {"Id": "20501", "Name": "ZenFone"},
                    ]
                },
                "Commercial": {"Items": []},
            },
            "Product": [],
        },
        "Status": "SUCCESS",
    }
    gaming_models = {
        "Result": {
            "ProductLevel": {"Products": {"Items": []}, "Commercial": {"Items": []}},
            "Product": [
                {"PDId": "15527", "PDHashedId": "byp9km39hwznmjc2", "PDName": "ROG Phone 5"},
                {"PDId": "20401", "PDHashedId": "rog6hash12345678", "PDName": "ROG Phone 6"},
                {"PDId": "22721", "PDHashedId": "v4hgayvwzqacmiam", "PDName": "ROG Phone 7"},
                {"PDId": "24493", "PDHashedId": "qyyqvkhdcff9gjvh", "PDName": "ROG Phone 8"},
                {"PDId": "30220", "PDHashedId": "rog9hash12345678", "PDName": "ROG Phone 9"},
            ],
        },
        "Status": "SUCCESS",
    }
    zenfone_models = {
        "Result": {
            "ProductLevel": {"Products": {"Items": []}, "Commercial": {"Items": []}},
            "Product": [
                {"PDId": "16100", "PDHashedId": "zf8hash123456789", "PDName": "Zenfone 8"},
                {"PDId": "21100", "PDHashedId": "zf9hash123456789", "PDName": "Zenfone 9"},
                {"PDId": "24002", "PDHashedId": "ebruneq86slbc1dj", "PDName": "Zenfone 10"},
                {"PDId": "25763", "PDHashedId": "lafw9hhn3uvry4ue", "PDName": "Zenfone 11 Ultra"},
            ],
        },
        "Status": "SUCCESS",
    }
    odin_payload = {
        "Status": "200",
        "Result": {
            "TotalCount": 2,
            "ProductList": [
                {"RealProductID": "25763", "ProductName": "ASUS Zenfone 11 Ultra", "ProductID": "lafw9hhn3uvry4ue"},
                {"RealProductID": "99901", "ProductName": "ROG Ally (2023)", "ProductID": "rogallyhash"},
            ],
        },
    }

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "www.asus.com" and req.url.path == "/support/api/product.asmx/GetPDLevel":
            qs = dict(req.url.params)
            if qs.get("type") == "1":
                return httpx.Response(200, json=series_payload)
            if qs.get("type") == "2" and qs.get("typeid") == "33240":
                return httpx.Response(200, json=gaming_models)
            if qs.get("type") == "2" and qs.get("typeid") == "20501":
                return httpx.Response(200, json=zenfone_models)
            return httpx.Response(200, json={"Result": {"Product": []}})
        if req.url.host == "odinapi.asus.com":
            return httpx.Response(200, json=odin_payload)
        if req.url.host == "www.asus.com" and req.url.path == "/support/webapi/ProductV2/GetPDBIOS":
            # Verify pdhashedid fallback works if pdid returns empty Obj
            if req.url.params.get("pdid") == "24002":
                return httpx.Response(200, json={"Result": {"Model": "", "Obj": []}})
            if req.url.params.get("pdhashedid") == "ebruneq86slbc1dj":
                return httpx.Response(
                    200,
                    json={
                        "Result": {
                            "Model": "ASUS_AI2302",
                            "Obj": [
                                {
                                    "Name": "Firmware",
                                    "Files": [
                                        {
                                            "Version": "WW-34.1004.0204.65",
                                            "Title": "ASUS Zenfone 10 Software Image",
                                            "Description": "Android 14",
                                            "FileSize": "4.80 GB",
                                            "ReleaseDate": "2026/05/12",
                                            "DownloadUrl": {
                                                "Global": "https://dlcdnets.asus.com/pub/ASUS/ZenFone/AI2302/UL-ASUS_AI2302-ASUS-34.1004.0204.65-1.1.1-user.zip"
                                            },
                                        }
                                    ],
                                }
                            ],
                        }
                    },
                )
        raise AssertionError(f"Unexpected request: {req.url}")

    adapter = AsusAdapter()
    with httpx.Client(transport=httpx.MockTransport(handler)) as raw_client:
        with OfficialMetadataHTTP(client=raw_client, allowed_hosts=OFFICIAL_METADATA_HOSTS) as http:
            devices = adapter.discover_devices(http)
            names = {d.marketing_name for d in devices}
            assert len(devices) == 9
            assert any("Zenfone 8" in n for n in names)
            assert any("Zenfone 9" in n for n in names)
            assert any("Zenfone 10" in n for n in names)
            assert any("Zenfone 11 Ultra" in n for n in names)
            assert any("ROG Phone 5" in n for n in names)
            assert any("ROG Phone 6" in n for n in names)
            assert any("ROG Phone 7" in n for n in names)
            assert any("ROG Phone 8" in n for n in names)
            assert any("ROG Phone 9" in n for n in names)
            assert not any("ROG Ally" in n for n in names)

            zf10 = next(d for d in devices if "Zenfone 10" in d.marketing_name)
            zf10_rels = adapter.fetch_releases(zf10, http)
            assert len(zf10_rels) == 1
            assert zf10_rels[0].model_code == "AI2302"
            assert zf10_rels[0].version_str == "WW-34.1004.0204.65"


# ---------------------------------------------------------------------------
# 5. Xiaomi Untagged Play-CSV Exclusion & Tagged Enrichment
# ---------------------------------------------------------------------------


def test_xiaomi_discover_devices_excludes_untagged_play_rows_and_enriches_tagged(
    tmp_path: Path,
) -> None:
    tags_yaml = tmp_path / "xiaomi_device_tags.yaml"
    tags_yaml.write_text(
        """
houji:
  tag: NC
  models: [23127PN0CC]
  android_versions: [14, 15]
  regions: [CN]
""",
        encoding="utf-8",
    )

    play_csv = (
        "Retail Branding,Marketing Name,Device,Model\n"
        "Xiaomi,Xiaomi 14,houji_global,23127PN0CG\n"
        "Xiaomi,Xiaomi 14,houji_eea_global,23127PN0CE\n"
        "Redmi,Redmi Note 9S,curtana,M2003J6A1G\n"
        "POCO,POCO X3 NFC,surya_global,M2007J20CG\n"
    ).encode("utf-16")

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "storage.googleapis.com":
            return httpx.Response(200, content=play_csv)
        return httpx.Response(404)

    adapter = XiaomiAdapter(device_tags_path=tags_yaml)
    with httpx.Client(transport=httpx.MockTransport(handler)) as raw_client:
        with OfficialMetadataHTTP(client=raw_client, allowed_hosts=OFFICIAL_METADATA_HOSTS) as http:
            devices = adapter.discover_devices(http)

    assert [d.codename for d in devices] == ["houji"]
    houji = devices[0]
    assert houji.marketing_name == "Xiaomi 14"
    assert set(houji.model_numbers) == {"23127PN0CC", "23127PN0CG", "23127PN0CE"}
    assert set(houji.vendor_meta["regions"]) == {"CN", "Global", "EEA"}
    assert houji.vendor_meta["tag"] == "NC"


# ---------------------------------------------------------------------------
# 6. Xiaomi Production Tags Expansion, Android 8-17 Seeds, Aliases & MiTV Filter
# ---------------------------------------------------------------------------


def test_xiaomi_production_tags_android_8_to_17_aliases_and_mitv_filter(
    tmp_path: Path,
) -> None:
    tags = load_xiaomi_device_tags()
    assert len(tags) >= 310
    assert tags["zircon"]["tag"] == "NO"
    assert isinstance(tags["zircon"]["tag"], str)
    assert tags["miro"]["tag"] == "OK"
    assert tags["zorn"]["tag"] == "OM"
    assert tags["houji"]["android_versions"] == [14, 15, 16]
    assert tags["dada"]["android_versions"] == [15, 16]
    assert tags["yingtian"]["android_versions"] == [17]

    expected_subs = {
        "sweet_k6a": "HG",
        "emerald_r": "FO",
        "rosemary_p": "FF",
        "shennong_t": "ND",
        "dandelion_c3l2": "CZ",
    }
    for sub_code, expected_tag in expected_subs.items():
        assert sub_code in tags
        assert tags[sub_code]["tag"] == expected_tag

    assert "marblein" in tags["marble"]["aliases"]
    assert "aresin" in tags["ares"]["aliases"]
    assert set(["rubyplus", "rubypro"]).issubset(set(tags["ruby"]["aliases"]))

    expected_seeds = {
        8: "V10.0.1.0.OCBCNXM",
        9: "V11.0.1.0.PCBCNXM",
        10: "V12.0.1.0.QCBCNXM",
        11: "V12.5.1.0.RCBCNXM",
        12: "V13.0.1.0.SCBCNXM",
        13: "V14.0.1.0.TCBCNXM",
        14: "V816.0.1.0.UCBCNXM",
        15: "OS2.0.1.0.VCBCNXM",
        16: "OS3.0.1.0.WCBCNXM",
        17: "OS4.0.1.0.XCBCNXM",
    }
    for android_major, expected_cv in expected_seeds.items():
        assert build_miota_seed_version(android_major, "CB", "CNXM") == expected_cv

    # Alias merging + MiTV/MiBOX/MiProj/Telebee row filter without dropping river phone row
    mini_yaml = tmp_path / "xiaomi_tags.yaml"
    mini_yaml.write_text(
        """
marble:
  tag: "MR"
  marketing_name: "Redmi Note 12 Turbo / POCO F5"
  models: ["23049RAD8C"]
  android_versions: [13, 14, 15]
  regions: [CN, Global]
  aliases: ["marblein"]
sky:
  tag: "MW"
  marketing_name: "Redmi Note 12R"
  models: ["23076RN8DY"]
  android_versions: [13, 14]
  regions: [CN, Global]
  aliases: ["river"]
cactus:
  tag: "CB"
  marketing_name: "Redmi 6A"
  models: ["Redmi 6A"]
  android_versions: [8, 9]
  regions: [CN]
""",
        encoding="utf-8",
    )

    play_csv = (
        "Retail Branding,Marketing Name,Device,Model\n"
        "POCO,POCO F5,marblein_in_global,23049PCD8I\n"
        "Redmi,Redmi 12 5G,river,23076RN4BI\n"
        "Xiaomi,MiTV-MZTU0,river,MiTV-MZTU0\n"
        "Xiaomi,MiBox S,once,MiBox S\n"
        "Xiaomi,MiProjA1,proj,MiProj A1\n"
        "Xiaomi,Telebee Stick,tbee,Telebee\n"
    ).encode("utf-16")

    seen_seed_cvs: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "storage.googleapis.com":
            return httpx.Response(200, content=play_csv)
        if str(req.url) == MIOTA_V3_URL and req.method == "POST":
            from urllib.parse import parse_qs

            qs = parse_qs(req.content.decode("utf-8"), keep_blank_values=True)
            req_json = decrypt_miota_payload(qs["q"][0])
            cv = req_json["options"]["cv"]
            seen_seed_cvs.append(cv)
            if cv == "V10.0.1.0.OCBCNXM":
                return httpx.Response(
                    200,
                    text=encrypt_miota_payload(
                        {
                            "LatestRom": {
                                "version": "V10.4.4.0.OCBCNXM",
                                "codebase": "8.1",
                                "filename": "miui_HM6A_V10.4.4.0.OCBCNXM_0123456789_8.1.zip",
                                "md5": "0123456789abcdef0123456789abcdef",
                                "filesize": "1500000000",
                            }
                        }
                    ),
                )
            return httpx.Response(200, text=encrypt_miota_payload({}))
        return httpx.Response(404)

    adapter = XiaomiAdapter(device_tags_path=mini_yaml)
    with httpx.Client(transport=httpx.MockTransport(handler)) as raw_client:
        with OfficialMetadataHTTP(client=raw_client, allowed_hosts=OFFICIAL_METADATA_HOSTS) as http:
            devices = {d.codename: d for d in adapter.discover_devices(http)}
            assert set(devices.keys()) == {"cactus", "marble", "sky"}
            assert "23049PCD8I" in devices["marble"].model_numbers
            assert "IN" in devices["marble"].vendor_meta["regions"]
            assert "marblein_in_global" in devices["marble"].vendor_meta["raw_codenames"]
            assert "23076RN4BI" in devices["sky"].model_numbers
            assert "MiTV-MZTU0" not in devices["sky"].model_numbers

            cactus_rels = adapter.fetch_releases(devices["cactus"], http)
            assert seen_seed_cvs == ["V10.0.1.0.OCBCNXM", "V11.0.1.0.PCBCNXM"]
            assert len(cactus_rels) == 1
            assert cactus_rels[0].version_str == "V10.4.4.0.OCBCNXM"


# ---------------------------------------------------------------------------
# 7. OPlus Expanded CN & Legacy Suffix Model Discovery
# ---------------------------------------------------------------------------


def test_oplus_expanded_cn_patterns_and_suffix_normalization() -> None:
    for cn_model in ("PGEM10", "PGKM10", "PMB110", "PMA110", "PYB110", "PNB110", "PEEM00", "PFUM10", "PDEM10"):
        assert OPLUS_OTA_MODEL_RE.fullmatch(cn_model), cn_model
        assert normalize_oplus_model(cn_model.lower()) == cn_model
        assert select_regions_for_model(cn_model) == ("CN",)

    assert normalize_oplus_model("opd2102a") == "OPD2102"
    assert normalize_oplus_model("OPD2102B") == "OPD2102"
    assert select_regions_for_model("OPD2102") == ("CN", "SG", "EU", "IN")

    assert normalize_oplus_model("cph1919ru") == "CPH1919"
    assert normalize_oplus_model("CPH1605FW") == "CPH1605"
    assert select_regions_for_model("CPH1919") == ("SG", "EU", "IN")
    assert select_regions_for_model("CPH1605") == ("SG", "EU", "IN")

    play_csv = (
        "Retail Branding,Marketing Name,Device,Model\n"
        "OPPO,OPPO Find X6,OP556FL1,PGEM10\n"
        "OPPO,OPPO Find N3 Flip,OP5963L1,PHT110\n"
        "OPPO,OPPO Find X8s,OP5D3FL1,PMB110\n"
        "OPPO,OPPO Reno14,OP5E11L1,PMA110\n"
        "OPPO,OPPO Pad 2,OP5925L1,OPD2102A\n"
        "OPPO,OPPO A9,OP4A79,CPH1919RU\n"
        "OPPO,OPPO F3,CPH1605,CPH1605FW\n"
        "OnePlus,OnePlus 13T,OP5E4DL1,PKX110\n"
    ).encode("utf-8")

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "storage.googleapis.com":
            return httpx.Response(200, content=play_csv)
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(handler)) as raw_client:
        with OfficialMetadataHTTP(client=raw_client, allowed_hosts=OFFICIAL_METADATA_HOSTS) as http:
            oppo_devs = OPlusAdapter("oppo").discover_devices(http)
            op_devs = OPlusAdapter("oneplus").discover_devices(http)

    oppo_models = {m for d in oppo_devs for m in d.model_numbers}
    op_models = {m for d in op_devs for m in d.model_numbers}
    assert {"PGEM10", "PMB110", "PMA110", "OPD2102", "CPH1919", "CPH1605"}.issubset(oppo_models)
    assert "PKX110" in op_models


# ---------------------------------------------------------------------------
# 8. Vivo Expanded Official Portals, Locale Paths & Accessory Filter
# ---------------------------------------------------------------------------


def test_vivo_expanded_portals_locale_paths_and_accessory_exclusion() -> None:
    assert len(DEFAULT_VIVO_REGIONS) >= 60
    assert "en" not in DEFAULT_VIVO_REGIONS
    assert VIVO_PORTAL_LOCALE_PATHS["ae"] == "ae/en"
    assert VIVO_PORTAL_LOCALE_PATHS["hk"] == "hk/zh"
    assert VIVO_PORTAL_LOCALE_PATHS["ua"] == "ua/uk"

    ae_home = """<!DOCTYPE html>
    <html><body>
      <a href="/ae/en/support/upgradePackageData?id=401">vivo X200 Pro</a>
      <a href="/ae/en/support/upgradePackageData?id=901">vivo TWS 3e</a>
      <a href="/ae/en/support/upgradePackageData?id=902">vivo Watch 3</a>
    </body></html>"""
    hk_home = """<!DOCTYPE html>
    <html><body>
      <a href="/hk/zh/support/upgradePackageData?id=401">vivo X200 Pro</a>
      <a href="/hk/zh/support/upgradePackageData?id=903">vivo FlashCharge Charger</a>
    </body></html>"""
    de_home = """<!DOCTYPE html>
    <html><body>
      <a href="/de/support/upgradePackageData?id=501">vivo V40 SE</a>
    </body></html>"""
    ae_data = """<!DOCTYPE html>
    <html><body>
      <span id="funtouchName">PD2405F_EX_A_15.0.2.1.W30.V000L1</span>
      <p>System: Funtouch OS 15 (Android 15)</p>
      <a href="https://sysupwksdl.vivoglobal.com/upgrade/official/PD2405F_EX_A_15.0.2.1.W30.V000L1-update-full.zip">Download</a>
    </body></html>"""

    seen_paths: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen_paths.append(req.url.path)
        if req.url.path == "/ae/en/support/upgradePackageHome":
            return httpx.Response(200, headers={"Content-Type": "text/html"}, text=ae_home)
        if req.url.path == "/hk/zh/support/upgradePackageHome":
            return httpx.Response(200, headers={"Content-Type": "text/html"}, text=hk_home)
        if req.url.path == "/de/support/upgradePackageHome":
            return httpx.Response(200, headers={"Content-Type": "text/html"}, text=de_home)
        if req.url.path == "/fr/support/upgradePackageHome":
            return httpx.Response(418, text="WAF Challenge")
        if req.url.path == "/ae/en/support/upgradePackageData" and req.url.params.get("id") == "401":
            return httpx.Response(200, headers={"Content-Type": "text/html"}, text=ae_data)
        return httpx.Response(404)

    adapter = VivoAdapter(regions=("ae", "hk", "de", "fr"))
    with httpx.Client(transport=httpx.MockTransport(handler)) as raw_client:
        with OfficialMetadataHTTP(client=raw_client, allowed_hosts=OFFICIAL_METADATA_HOSTS) as http:
            devices = {d.codename: d for d in adapter.discover_devices(http)}
            assert "/ae/en/support/upgradePackageHome" in seen_paths
            assert "/hk/zh/support/upgradePackageHome" in seen_paths
            assert "/de/support/upgradePackageHome" in seen_paths
            assert set(devices.keys()) == {"vivo-x200-pro", "vivo-v40-se"}
            assert "vivo-tws-3e" not in devices
            assert "vivo-watch-3" not in devices
            assert devices["vivo-x200-pro"].vendor_meta["region_model_ids"] == {"ae": "401", "hk": "401"}

            rels = adapter.fetch_releases(devices["vivo-x200-pro"], http)
            assert len(rels) == 1
            assert rels[0].region_code == "AE"
            assert "/ae/en/support/upgradePackageData" in seen_paths
