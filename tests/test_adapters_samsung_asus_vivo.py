"""Offline unit tests for official Samsung, ASUS/ROG, and vivo VendorAdapter stations."""

from __future__ import annotations

import json

import httpx
import pytest

from fmr_ingest_stock import download_refresh
from fmr_ingest_stock.adapters.asus import AsusAdapter, build_asus_slug
from fmr_ingest_stock.adapters.base import OFFICIAL_METADATA_HOSTS, OfficialMetadataHTTP
from fmr_ingest_stock.adapters.samsung import SamsungAdapter
from fmr_ingest_stock.adapters.vivo import VivoAdapter
from fmr_ingest_stock.samsung_fus import parse_version_xml
from fmr_ingest_stock.samsung_metadata import parse as parse_samsung_notice

TEST_ALLOWED_HOSTS = frozenset(
    {
        *OFFICIAL_METADATA_HOSTS,
        "www.asus.com",
        "odinapi.asus.com",
        "www.vivo.com",
    }
)


def test_samsung_adapter_discovery_two_step_doc_and_smartfus_resolution() -> None:
    play_csv = (
        "Retail Branding,Marketing Name,Device,Model\n"
        "Samsung,Galaxy S24 Ultra,e3q,SM-S9280\n"
        "Samsung,Galaxy S24 Ultra,e3q,SM-S928B/DS\n"
        "Samsung,Galaxy Watch5,freshbl,SM-R900\n"
        "Samsung,Galaxy Watch7,fresh7,SM-L300\n"
        "Samsung,Galaxy Buds2 Pro,berry,SM-R510\n"
        "Google,Pixel 9,tokay,GEC77\n"
    ).encode("utf-16")

    tgy_version_xml = """<?xml version="1.0" encoding="UTF-8"?>
    <versioninfo>
      <firmware>
        <model>SM-S9280</model>
        <cc>TGY</cc>
        <version>
          <latest o="17">S9280ZHS4AXK1/S9280OZS4AXK1/S9280ZCU4AXK1</latest>
          <upgrade>
            <value>S9280ZHS3AWJ1/S9280OZS3AWJ1/S9280ZCU3AWJ1</value>
          </upgrade>
        </version>
      </firmware>
    </versioninfo>
    """

    chc_version_xml = """<?xml version="1.0" encoding="UTF-8"?>
    <versioninfo>
      <firmware>
        <model>SM-S9280</model>
        <cc>CHC</cc>
        <version>
          <latest o="17">S9280ZCU4AXK9/S9280CHC4AXK9/S9280ZCU4AXK9</latest>
        </version>
      </firmware>
    </versioninfo>
    """

    tgy_doc_selector_html = """<!DOCTYPE html>
    <html><body>
      <input type="hidden" id="dflt_page" value="../../SM-S9280/029233240224/chi.html" />
      <select id="sel_lang">
        <option value="../../SM-S9280/029233240224/chi.html">Traditional Chinese</option>
        <option value="../../SM-S9280/029233240224/eng.html">English</option>
      </select>
    </body></html>
    """

    tgy_eng_notice_html = """<!DOCTYPE html>
    <html><body>
      <div>Build Number : S9280ZHS4AXK1</div>
      <div>Android version : V(Android 15)</div>
      <div>Release Date : 2026-10-28</div>
      <div>Security patch level : 2026-10-01</div>
      <div>Build Number : S9280ZHS4AXK1</div>
      <div>Android version : V(Android 15)</div>
      <div>Security patch level : 2026-10-01</div>
    </body></html>
    """

    seen_urls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen_urls.append(str(req.url))
        host = req.url.host
        path = req.url.path

        if host == "storage.googleapis.com" and path == "/play_public/supported_devices.csv":
            return httpx.Response(200, content=play_csv)

        if host == "fota-cloud-dn.ospserver.net":
            # Enforce live Akamai quirk: Kies2.0_FUS User-Agent is mandatory
            if req.headers.get("User-Agent") != "Kies2.0_FUS":
                return httpx.Response(403, text="Forbidden")
            if path == "/firmware/TGY/SM-S9280/version.xml":
                return httpx.Response(200, headers={"Content-Type": "application/xml"}, text=tgy_version_xml)
            if path == "/firmware/CHC/SM-S9280/version.xml":
                return httpx.Response(200, headers={"Content-Type": "application/xml"}, text=chc_version_xml)
            return httpx.Response(404, text="Not Found")

        if host == "doc.samsungmobile.com":
            # Direct /{model}/{csc}/eng.html returns HTTP 403 in production
            if path == "/SM-S9280/TGY/eng.html":
                return httpx.Response(403, text="Forbidden")
            if path == "/SM-S9280/TGY/doc.html":
                return httpx.Response(200, headers={"Content-Type": "text/html"}, text=tgy_doc_selector_html)
            if path == "/SM-S9280/029233240224/eng.html":
                return httpx.Response(200, headers={"Content-Type": "text/html"}, text=tgy_eng_notice_html)
            return httpx.Response(404, text="Not Found")

        if host == "neofussvr.sslcs.cdngc.net":
            if path == "/NF_SmartDownloadGenerateNonce.do":
                return httpx.Response(
                    200,
                    headers={
                        "nonce": "abcdefghijklmnop",
                        "Set-Cookie": "JSESSIONID=fus_cookie_abc123; Path=/; HttpOnly",
                    },
                    content=b"",
                )
            if path == "/NF_SmartDownloadBinaryInform.do":
                assert "JSESSIONID=fus_cookie_abc123" in req.headers.get("Cookie", "")
                body_str = req.content.decode("utf-8")
                if "S9280ZHS3AWJ1/S9280OZS3AWJ1/S9280ZCU3AWJ1/S9280ZHS3AWJ1" in body_str:
                    return httpx.Response(
                        200,
                        headers={"Content-Type": "application/xml"},
                        text="<FUSMsg><FUSBody><Results><Status>F00</Status></Results></FUSBody></FUSMsg>",
                    )
                if "S9280ZHS4AXK1/S9280OZS4AXK1/S9280ZCU4AXK1/S9280ZHS4AXK1" in body_str:
                    return httpx.Response(
                        200,
                        headers={"Content-Type": "application/xml"},
                        text="""<FUSMsg><FUSBody><Results><Status>200</Status></Results><Put>
                        <BINARY_SW_VERSION><Data>S9280ZHS4AXK1/S9280OZS4AXK1/S9280ZCU4AXK1/S9280ZHS4AXK1</Data></BINARY_SW_VERSION>
                        <BINARY_NAME><Data>SM-S9280_4_20261025120000_xyz_fac.zip.enc4</Data></BINARY_NAME>
                        <MODEL_PATH><Data>/SmartSwitch/SM-S9280/</Data></MODEL_PATH>
                        <BINARY_BYTE_SIZE><Data>8589934592</Data></BINARY_BYTE_SIZE>
                        </Put></FUSBody></FUSMsg>""",
                    )
                if "S9280ZCU4AXK9/S9280CHC4AXK9/S9280ZCU4AXK9/S9280ZCU4AXK9" in body_str:
                    return httpx.Response(
                        200,
                        headers={"Content-Type": "application/xml"},
                        text="""<FUSMsg><FUSBody><Results><Status>S00</Status></Results><Put>
                        <BINARY_SW_VERSION><Data>S9280ZCU4AXK9/S9280CHC4AXK9/S9280ZCU4AXK9/S9280ZCU4AXK9</Data></BINARY_SW_VERSION>
                        <BINARY_NAME><Data>SM-S9280_4_20261107190140_chc_fac.zip.enc4</Data></BINARY_NAME>
                        <MODEL_PATH><Data>/SmartSwitch/SM-S9280/</Data></MODEL_PATH>
                        <BINARY_BYTE_SIZE><Data>9126805504</Data></BINARY_BYTE_SIZE>
                        </Put></FUSBody></FUSMsg>""",
                    )
            return httpx.Response(500, text="Unexpected FUS request")

        raise AssertionError(f"Unexpected host requested: {req.url}")

    adapter = SamsungAdapter(cscs=("TGY", "CHC", "EUX"))
    with httpx.Client(transport=httpx.MockTransport(handler)) as raw_client:
        with OfficialMetadataHTTP(client=raw_client, allowed_hosts=TEST_ALLOWED_HOSTS) as http:
            devices = adapter.discover_devices(http)
            assert len(devices) == 1
            dev = devices[0]
            assert dev.brand_id == "samsung"
            assert dev.marketing_name == "Samsung Galaxy S24 Ultra"
            assert dev.model_numbers == ("SM-S9280", "SM-S928B")
            assert dev.vendor_meta["models"] == ["SM-S9280", "SM-S928B"]
            assert "SM-R900" not in dev.model_numbers
            assert "SM-L300" not in dev.model_numbers

            releases = adapter.fetch_releases(dev, http)
            # TGY latest + CHC latest succeed; TGY upgrade F00 and EUX 404 are skipped gracefully
            assert len(releases) == 2

            by_region = {r.region_code: r for r in releases}
            tgy_rel = by_region["TGY"]
            assert tgy_rel.model_code == "SM-S9280"
            assert tgy_rel.package_type == "samsung_encrypted"
            assert tgy_rel.delivery_mode == "resolver_fus"
            assert tgy_rel.verification_status == "VERIFIED_RESOLVER"
            assert tgy_rel.android_version == "15"
            # Duplicate AP block in tgy_eng_notice_html omitted Release Date in block #2,
            # verifying that non-contradictory non-null date 2026-10-28 was preserved!
            assert tgy_rel.published_date == "2026-10-28"
            assert tgy_rel.security_patch == "2026-10-01"
            assert tgy_rel.os_name == "One UI"
            assert tgy_rel.file_size_bytes == 8589934592
            assert tgy_rel.file_size_str == "8.00 GB"
            assert tgy_rel.vendor_fields == {
                "ap": "S9280ZHS4AXK1",
                "csc_version": "S9280OZS4AXK1",
                "cp": "S9280ZCU4AXK1",
            }
            assert tgy_rel.resolver_meta is not None
            source_desc = tgy_rel.resolver_meta["source"]
            expected_url = f"https://findmyrom.com/stock/refresh/{download_refresh.fingerprint(source_desc)}"
            assert tgy_rel.original_official_url == expected_url
            assert tgy_rel.fast_cdn_url == expected_url

            # CHC had no doc.html notice -> falls back to <latest o="17"> and BINARY_NAME YYYYMMDD!
            chc_rel = by_region["CHC"]
            assert chc_rel.android_version == "17"
            assert chc_rel.published_date == "2026-11-07"
            assert chc_rel.security_patch is None
            assert chc_rel.original_filename == "SM-S9280_4_20261107190140_chc_fac.zip.enc4"

            # Verify resolve_delivery idempotency and PublishSpecV1 contract compliance
            resolved_again = adapter.resolve_delivery(chc_rel, http)
            assert resolved_again == chc_rel
            spec = tgy_rel.to_publish_spec(dev)
            assert spec.release.package_type == "samsung_encrypted"
            assert spec.device.codename == dev.codename

    assert "https://doc.samsungmobile.com/SM-S9280/029233240224/eng.html" in seen_urls
    assert "https://doc.samsungmobile.com/SM-S9280/TGY/eng.html" not in seen_urls


def test_samsung_version_xml_rejects_xxe_and_duplicate_ap_metadata_merge() -> None:
    with pytest.raises(ValueError, match="Unsafe Samsung version.xml"):
        parse_version_xml(b'<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><versioninfo/>')

    # Non-null fields preserved when duplicate AP has missing field; cleared only on true conflict
    html = (
        "<div>Build Number : S928BXXS4AXK1</div>"
        "<div>Android version : U(Android 14)</div>"
        "<div>Release Date : 2026-08-12</div>"
        "<div>Build Number : S928BXXS4AXK1</div>"
        "<div>Android version : U(Android 14)</div>"
        "<div>Security patch level : 2026-08-01</div>"
    )
    parsed = parse_samsung_notice(html, "SM-S928B", "EUX", "https://doc.samsungmobile.com/SM-S928B/EUX/doc.html")
    assert parsed["S928BXXS4AXK1"]["android"] == "14"
    assert parsed["S928BXXS4AXK1"]["date"] == "2026-08-12"
    assert parsed["S928BXXS4AXK1"]["patch"] == "2026-08-01"


def test_asus_adapter_discovery_rog_brand_split_and_pdbios_parsing() -> None:
    series_json = {
        "Status": "SUCCESS",
        "Result": {
            "Product": [
                {"Id": "101", "Name": "ZenFone"},
                {"Id": "102", "Name": "ROG Phone"},
                {"Id": "103", "Name": "ASUS VivoWatch"},
            ]
        },
    }
    zenfone_models_json = {
        "Status": "SUCCESS",
        "Result": {
            "Product": [
                {"PDId": "29001", "PDName": "Zenfone 11 Ultra", "PDHashedId": "hash_zf11u"},
                {"PDId": "29999", "PDName": "ASUS Chromebook Flip", "PDHashedId": "hash_cb"},
            ]
        },
    }
    rog_models_json = {
        "Status": "SUCCESS",
        "Result": {
            "Product": [
                {"PDId": "30200", "PDName": "ROG Phone 9", "PDHashedId": "hash_rog9"},
            ]
        },
    }
    odin_json = {
        "Status": "SUCCESS",
        "Result": {
            "TotalCount": 3,
            "ProductList": [
                # Duplicate of Zenfone 11 Ultra -> should deduplicate cleanly
                {"RealProductID": "29001", "ProductName": "ASUS Zenfone 11 Ultra", "ProductID": "hash_zf11u"},
                # New storefront SKU not yet in GetPDLevel
                {"RealProductID": "30201", "ProductName": "ROG Phone 9 Pro", "ProductID": "hash_rog9p", "ModelSpecName": "AI2501"},
                # Accessory excluded
                {"RealProductID": "30999", "ProductName": "AeroActive Cooler X", "ProductID": "hash_acc"},
            ],
        },
    }

    rog9_pdbios_json = {
        "Status": "SUCCESS",
        "Result": {
            "Model": "ASUS_AI2501",
            "Obj": [
                {
                    "Name": "Firmware",
                    "Files": [
                        {
                            "Version": "ZWW-36.0210.1420.56",
                            "Title": "ASUS ROG Phone 9 (AI2501) Software Image: ZWW-36.0210.1420.56 for WW SKU only",
                            "Description": "Improved system stability.<br>Android 15<br>SHA256: ABCDEF0123456789ABCDEF0123456789ABCDEF0123456789ABCDEF0123456789",
                            "FileSize": "5.50 GBytes",
                            "ReleaseDate": "2026/11/10",
                            "DownloadUrl": {
                                "Global": "/pub/ASUS/ZenFone/AI2501/UL-ASUS_AI2501-ASUS-36.0210.1420.56-1.1.1-user.zip?model=ROG Phone 9",
                                "China": "",
                            },
                        },
                        {
                            "Version": "V11.2.0",
                            "Title": "Game Genie APK Utility",
                            "Description": "Game Genie update",
                            "FileSize": "45.2 MBytes",
                            "ReleaseDate": "2026/11/01",
                            "DownloadUrl": {
                                "Global": "https://dlcdnets.asus.com/pub/ASUS/ZenFone/AI2501/GameGenie_v11.zip",
                            },
                        },
                        {
                            "Version": "V1.0",
                            "Title": "Unlock Device App",
                            "Description": "Bootloader unlock APK",
                            "FileSize": "4.0 MBytes",
                            "ReleaseDate": "2026/10/01",
                            "DownloadUrl": {
                                "Global": "https://dlcdnets.asus.com/pub/ASUS/ZenFone/AI2501/UnlockTool.apk",
                            },
                        },
                    ],
                },
                {
                    "Name": "Utilities",
                    "Files": [
                        {
                            "Version": "V2.0",
                            "Title": "ASUS USB Driver",
                            "FileSize": "12 MBytes",
                            "ReleaseDate": "2026/09/01",
                            "DownloadUrl": {
                                "Global": "https://dlcdnets.asus.com/pub/ASUS/ZenFone/AI2501/USB_Driver.zip",
                            },
                        }
                    ],
                },
            ],
        },
    }

    zf11_pdbios_json = {
        "Status": "SUCCESS",
        "Result": {
            "Model": "",
            "Obj": [
                {
                    "Name": "BIOS",
                    "Files": [
                        {
                            "Version": "EU-34.1420.1420.407",
                            "Title": "Zenfone 11 Ultra Firmware",
                            "Description": "Android 14 security update",
                            "CheckSum": "0123456789ABCDEF0123456789ABCDEF0123456789ABCDEF0123456789ABCDEF",
                            "FileSize": "5.10 GB",
                            "ReleaseDate": "2026/09/15",
                            "DownloadUrl": {
                                "Global": "https://dlcdnets.asus.com/pub/ASUS/ZenFone/AI2401/UL-ASUS_AI2401-ASUS-34.1420.1420.407-user.zip",
                            },
                        }
                    ],
                }
            ],
        },
    }

    def handler(req: httpx.Request) -> httpx.Response:
        url_str = str(req.url)
        if req.url.host == "www.asus.com" and req.url.path == "/support/api/product.asmx/GetPDLevel":
            assert req.headers.get("Origin") == "https://www.asus.com"
            assert req.headers.get("Referer") == "https://www.asus.com/"
            qs = dict(req.url.params)
            if qs.get("type") == "1":
                return httpx.Response(200, json=series_json)
            if qs.get("type") == "2" and qs.get("typeid") == "101":
                return httpx.Response(200, json=zenfone_models_json)
            if qs.get("type") == "2" and qs.get("typeid") == "102":
                return httpx.Response(200, json=rog_models_json)
            return httpx.Response(200, json={"Result": {"Product": []}})

        if req.url.host == "odinapi.asus.com" and req.url.path == "/recent-data/apiv2/SeriesFilterResult":
            return httpx.Response(200, json=odin_json)

        if req.url.host == "www.asus.com" and req.url.path == "/support/webapi/ProductV2/GetPDBIOS":
            assert req.headers.get("Referer") == "https://www.asus.com/"
            pdid = req.url.params.get("pdid")
            if pdid == "30200":
                return httpx.Response(200, json=rog9_pdbios_json)
            if pdid == "29001":
                return httpx.Response(200, json=zf11_pdbios_json)
            return httpx.Response(200, json={"Result": {"Obj": []}})

        raise AssertionError(f"Unexpected ASUS request: {url_str}")

    assert build_asus_slug("ROG Phone 9 Pro") == "ROG-Phone-9-Pro"

    adapter = AsusAdapter()
    with httpx.Client(transport=httpx.MockTransport(handler)) as raw_client:
        with OfficialMetadataHTTP(client=raw_client, allowed_hosts=TEST_ALLOWED_HOSTS) as http:
            devices = adapter.discover_devices(http)
            assert len(devices) == 3

            by_pdid = {d.vendor_meta["pdid"]: d for d in devices}
            zf11 = by_pdid["29001"]
            rog9 = by_pdid["30200"]
            rog9p = by_pdid["30201"]

            assert zf11.brand_id == "asus"
            assert zf11.vendor_meta["slug"] == "Zenfone-11-Ultra"
            assert rog9.brand_id == "rog"
            assert rog9.vendor_meta["slug"] == "ROG-Phone-9"
            assert rog9p.brand_id == "rog"
            assert "AI2501" in rog9p.model_numbers

            rog_releases = adapter.fetch_releases(rog9, http)
            assert len(rog_releases) == 1
            rel = rog_releases[0]
            assert rel.model_code == "AI2501"
            assert rel.region_code == "WW"
            assert rel.version_str == "ZWW-36.0210.1420.56"
            assert rel.android_version == "15"
            assert rel.published_date == "2026-11-10"
            assert rel.package_type == "recovery_zip"
            assert rel.delivery_mode == "direct_cdn"
            assert rel.verification_status == "VERIFIED_IMMUTABLE"
            assert rel.md5_or_sha256 == "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789"
            assert (
                rel.original_official_url
                == "https://dlcdnets.asus.com/pub/ASUS/ZenFone/AI2501/UL-ASUS_AI2501-ASUS-36.0210.1420.56-1.1.1-user.zip?model=ROG%20Phone%209"
            )
            assert rel.file_size_bytes is not None and rel.file_size_bytes > 5_000_000_000

            rog_spec = rel.to_publish_spec(rog9)
            assert rog_spec.device.brand_id == "rog"

            zf_releases = adapter.fetch_releases(zf11, http)
            assert len(zf_releases) == 1
            zf_rel = zf_releases[0]
            assert zf_rel.model_code == "AI2401"
            assert zf_rel.region_code == "EU"
            assert zf_rel.published_date == "2026-09-15"
            assert zf_rel.md5_or_sha256 == "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
            zf_spec = zf_rel.to_publish_spec(zf11)
            assert zf_spec.device.brand_id == "asus"


def test_vivo_adapter_full_zip_extraction_and_pdf_only_graceful_skip() -> None:
    in_home_html = """<!DOCTYPE html>
    <html><body>
      <div class="model-list">
        <a href="/in/support/upgradePackageData?id=227">X100 Pro</a>
        <a href="/in/support/upgradePackageData?id=246">V40 Pro</a>
      </div>
    </body></html>
    """
    en_home_html = """<!DOCTYPE html>
    <html><body>
      <div class="model-list">
        <a href="/en/support/upgradePackageData?id=227">vivo X100 Pro</a>
      </div>
    </body></html>
    """

    id_227_html = """<!DOCTYPE html>
    <html><body>
      <div class="pkg-info">
        <span id="funtouchName">PD2337F_EX_A_14.0.7.10.W30.V000L1</span>
        <p>Release date : 2026-10-15</p>
        <p>File size : 6.85 GB</p>
        <p>System : Funtouch OS 14 (Android 14)</p>
        <a class="download-btn" href="https://sysupwksdl.vivoglobal.com/upgrade/official/PD2337F_EX_A_14.0.7.10.W30.V000L1-update-full_1729000000.zip">Download</a>
        <a class="guide-btn" href="https://www.vivo.com/in/support/tutorial.pdf">Tutorial PDF</a>
      </div>
    </body></html>
    """

    id_246_pdf_only_html = """<!DOCTYPE html>
    <html><body>
      <div class="pkg-info">
        <h2>OTA Upgrade Only</h2>
        <p>Please update your device via Settings &gt; System Update.</p>
        <a href="https://asia-exstatic-vivofs.vivo.com/instructions/V40_Pro_System_Update_Guide.pdf">Download PDF Guide</a>
      </div>
    </body></html>
    """

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.host == "www.vivo.com"
        path = req.url.path
        model_id = req.url.params.get("id")
        if path == "/in/support/upgradePackageHome":
            return httpx.Response(200, headers={"Content-Type": "text/html"}, text=in_home_html)
        if path == "/en/support/upgradePackageHome":
            return httpx.Response(200, headers={"Content-Type": "text/html"}, text=en_home_html)
        if path in ("/in/support/upgradePackageData", "/en/support/upgradePackageData"):
            if model_id == "227":
                return httpx.Response(200, headers={"Content-Type": "text/html"}, text=id_227_html)
            if model_id == "246":
                return httpx.Response(200, headers={"Content-Type": "text/html"}, text=id_246_pdf_only_html)
        return httpx.Response(404, text="Not Found")

    adapter = VivoAdapter(regions=("in", "en"))
    with httpx.Client(transport=httpx.MockTransport(handler)) as raw_client:
        with OfficialMetadataHTTP(client=raw_client, allowed_hosts=TEST_ALLOWED_HOSTS) as http:
            devices = adapter.discover_devices(http)
            assert len(devices) == 2

            by_name = {d.marketing_name: d for d in devices}
            x100 = by_name["vivo X100 Pro"]
            v40 = by_name["vivo V40 Pro"]

            # Regional portals ("in" + "en") grouped into one canonical device
            assert x100.vendor_meta["region_model_ids"] == {"in": "227", "en": "227"}

            x100_releases = adapter.fetch_releases(x100, http)
            # Identical (version, zip_url) across in/en is deduplicated cleanly
            assert len(x100_releases) == 1
            rel = x100_releases[0]
            assert rel.model_code == "PD2337F"
            assert rel.version_str == "PD2337F_EX_A_14.0.7.10.W30.V000L1"
            assert rel.android_version == "14"
            assert rel.os_name == "Funtouch OS"
            assert rel.os_version == "14"
            assert rel.published_date == "2026-10-15"
            assert rel.package_type == "recovery_zip"
            assert rel.delivery_mode == "direct_cdn"
            assert rel.verification_status == "VERIFIED_IMMUTABLE"
            assert (
                rel.original_official_url
                == "https://sysupwksdl.vivoglobal.com/upgrade/official/PD2337F_EX_A_14.0.7.10.W30.V000L1-update-full_1729000000.zip"
            )
            assert rel.file_size_bytes is not None and rel.file_size_bytes > 6_000_000_000
            spec = rel.to_publish_spec(x100)
            assert spec.device.brand_id == "vivo"

            # PDF-only page (id=246) returns [] cleanly without error
            assert adapter.fetch_releases(v40, http) == []


def test_edge_cases_dflt_page_cold_resolve_and_third_party_url_rejection() -> None:
    from fmr_ingest_stock.adapters.base import AdapterDevice, AdapterRelease
    from fmr_ingest_stock.samsung_fus import FirmwareUnavailable, SmartFUS
    from fmr_ingest_stock.samsung_metadata import resolve_notice_url

    # 1. Samsung #dflt_page redirect fallback & cross-model rejection
    dflt_html = '<html><body><input id="dflt_page" value="../../SM-S928B/0112233/kor.html" /></body></html>'
    assert (
        resolve_notice_url("https://doc.samsungmobile.com/SM-S928B/KOO/doc.html", dflt_html, "SM-S928B")
        == "https://doc.samsungmobile.com/SM-S928B/0112233/kor.html"
    )
    bad_html = '<html><body><input id="dflt_page" value="../../SM-OTHER/0112233/eng.html" /></body></html>'
    with pytest.raises(ValueError, match="Mismatched Samsung update model"):
        resolve_notice_url("https://doc.samsungmobile.com/SM-S928B/KOO/doc.html", bad_html, "SM-S928B")

    # 2. SmartFUS F11 and HTTP 404 raise FirmwareUnavailable
    for mode in ("F11", "http404"):
        def fus_err_handler(req: httpx.Request) -> httpx.Response:
            if req.url.path.endswith("GenerateNonce.do"):
                return httpx.Response(200, headers={"nonce": "abcdefghijklmnop"})
            if mode == "http404":
                return httpx.Response(404, text="Not Found")
            return httpx.Response(
                200,
                headers={"Content-Type": "text/xml"},
                text="<FUSMsg><FUSBody><Results><Status>F11</Status></Results></FUSBody></FUSMsg>",
            )

        with httpx.Client(transport=httpx.MockTransport(fus_err_handler)) as c:
            with pytest.raises(FirmwareUnavailable):
                SmartFUS(c).binary_info("SM-S928B", "EUX", "PDA/CSC/MODEM")

    # 3. ASUS & vivo reject third-party download URLs strictly
    def reject_handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "www.asus.com":
            return httpx.Response(
                200,
                json={
                    "Result": {
                        "Model": "ASUS_AI2401",
                        "Obj": [
                            {
                                "Name": "Firmware",
                                "Files": [
                                    {
                                        "Version": "WW-34.1420.1420.100",
                                        "Title": "Firmware",
                                        "DownloadUrl": {"Global": "https://evil-mirror.example.com/rom.zip"},
                                    }
                                ],
                            }
                        ],
                    }
                },
            )
        if req.url.host == "www.vivo.com":
            return httpx.Response(
                200,
                headers={"Content-Type": "text/html"},
                text='<span id="funtouchName">PD2337F_EX_A_14.1</span><a href="https://mega.nz/PD2337F.zip">Download</a>',
            )
        return httpx.Response(404)

    asus_dev = AdapterDevice(
        codename="asus-zenfone-11-ultra",
        brand_id="asus",
        marketing_name="ASUS Zenfone 11 Ultra",
        vendor_meta={"pdid": "29001", "slug": "Zenfone-11-Ultra"},
    )
    vivo_dev = AdapterDevice(
        codename="vivo-x100-pro",
        brand_id="vivo",
        marketing_name="vivo X100 Pro",
        vendor_meta={"region_model_ids": {"in": "227"}},
    )
    with httpx.Client(transport=httpx.MockTransport(reject_handler)) as raw_client:
        with OfficialMetadataHTTP(client=raw_client, allowed_hosts=TEST_ALLOWED_HOSTS) as http:
            assert AsusAdapter().fetch_releases(asus_dev, http) == []
            assert VivoAdapter(regions=("in",)).fetch_releases(vivo_dev, http) == []

    # 4. Cold-start SamsungAdapter.resolve_delivery populates refresh URL and size
    def cold_fus_handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("GenerateNonce.do"):
            return httpx.Response(200, headers={"nonce": "abcdefghijklmnop"})
        return httpx.Response(
            200,
            headers={"Content-Type": "application/xml"},
            text="""<FUSMsg><FUSBody><Results><Status>S00</Status></Results><Put>
            <BINARY_SW_VERSION><Data>S928BXXS4AXK1/S928BOXM4AXK1/S928BXXS4AXK1/S928BXXS4AXK1</Data></BINARY_SW_VERSION>
            <BINARY_NAME><Data>SM-S928B_1_20261108093000_eux_fac.zip.enc4</Data></BINARY_NAME>
            <MODEL_PATH><Data>/SmartSwitch/SM-S928B/</Data></MODEL_PATH>
            <BINARY_BYTE_SIZE><Data>1073741824</Data></BINARY_BYTE_SIZE>
            </Put></FUSBody></FUSMsg>""",
        )

    unresolved = AdapterRelease(
        device_codename="samsung-e3q",
        model_code="SM-S928B",
        region_code="EUX",
        version_str="S928BXXS4AXK1",
        raw_version="S928BXXS4AXK1/S928BOXM4AXK1/S928BXXS4AXK1",
        android_version="15",
        package_type="samsung_encrypted",
        original_official_url="https://fota-cloud-dn.ospserver.net/firmware/EUX/SM-S928B/version.xml",
        fast_cdn_url="https://fota-cloud-dn.ospserver.net/firmware/EUX/SM-S928B/version.xml",
    )
    with httpx.Client(transport=httpx.MockTransport(cold_fus_handler)) as raw_client:
        with OfficialMetadataHTTP(client=raw_client, allowed_hosts=TEST_ALLOWED_HOSTS) as http:
            resolved = SamsungAdapter().resolve_delivery(unresolved, http)
            assert resolved.delivery_mode == "resolver_fus"
            assert resolved.verification_status == "VERIFIED_RESOLVER"
            assert resolved.published_date == "2026-11-08"
            assert resolved.file_size_bytes == 1073741824
            assert resolved.file_size_str == "1.00 GB"
            assert resolved.original_official_url.startswith("https://findmyrom.com/stock/refresh/")

