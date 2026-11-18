"""Offline unit & v4 end-to-end tests for SonyAdapter and Sony seed manifest."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import yaml

from fmr_core import firmware_metadata_schema
from fmr_core.crypto import generate_keypair
from fmr_core.d1_client import D1Client
from fmr_core.schema_migrations import ensure_schema
from fmr_ingest_stock.adapters import get_adapter
from fmr_ingest_stock.adapters.base import OfficialMetadataHTTP
from fmr_ingest_stock.adapters.sony import SonyAdapter, load_sony_customizations
from fmr_ingest_stock.pipeline import OutboxSpool, run_collector, run_publisher
from fmr_ingest_stock.sony_bundle import load_bundle_record
from fmr_ingest_stock.sony_source import manifest_digest

SCHEMA_SQL_PATH = Path(__file__).resolve().parents[1] / "fmr_core" / "schema.sql"


def test_sony_customizations_seed_has_only_phones_and_tablets() -> None:
    seeds = load_sony_customizations()
    assert len(seeds) >= 200
    assert "XQ-GE74" in seeds
    assert "SOG02" in seeds
    assert "XQ-AT51" in seeds
    assert any(c.get("spc") == "43041672" for c in seeds["XQ-GE74"]["custs"])
    for model_name, meta in seeds.items():
        assert "smartwatch" not in meta["label"].lower()
        assert "transmitter" not in meta["label"].lower()
        assert meta["custs"], model_name


def test_sony_adapter_gen1_gen2_v4_pipeline_and_r2_lookup(tmp_path: Path) -> None:
    seed_doc = {
        "schema_version": 1,
        "products": {
            "PDX-267": {
                "label": "Xperia 1 VIII",
                "device_type": "phone",
                "year": 2026,
                "models": {
                    "XQ-GE74": [
                        {"id": "28657952", "name": "Customized HK", "spc": "43041672"},
                    ]
                },
            },
            "PDX-203": {
                "label": "Xperia 1 II",
                "device_type": "phone",
                "year": 2020,
                "models": {
                    "XQ-AT51": [
                        {"id": "1321-7192", "name": "Customized UK"},
                        {"id": "1320-9999", "name": "Retired Region"},
                    ]
                },
            },
            "Brooks": {
                "label": "SmartWatch 3",
                "device_type": "phone",
                "year": 2015,
                "models": {
                    "SWR50": [{"id": "1234-5678", "name": "Watch"}],
                },
            },
        },
    }
    seed_path = tmp_path / "sony_seed.yaml"
    seed_path.write_text(yaml.safe_dump(seed_doc), encoding="utf-8")

    product_v2_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <ns2:product-list xmlns:ns2="http://xmlns.sony.net/ess">
      <products>
        <product><id>pid-267</id><name>PDX-267</name></product>
        <product><id>pid-203</id><name>PDX-203</name></product>
        <product><id>pid-watch</id><name>Brooks</name></product>
      </products>
      <hwvariants>
        <hwvariant><id>hw-ge74</id><productId>pid-267</productId></hwvariant>
        <hwvariant><id>hw-at51</id><productId>pid-203</productId></hwvariant>
        <hwvariant><id>hw-swr50</id><productId>pid-watch</productId></hwvariant>
      </hwvariants>
      <models>
        <model><id>mid-ge74</id><name>XQ-GE74</name><hwVariantId>hw-ge74</hwVariantId><isModelEOS>false</isModelEOS></model>
        <model><id>mid-at51</id><name>XQ-AT51</name><hwVariantId>hw-at51</hwVariantId><isModelEOS>true</isModelEOS></model>
        <model><id>mid-swr50</id><name>SWR50</name><hwVariantId>hw-swr50</hwVariantId><isModelEOS>true</isModelEOS></model>
      </models>
    </ns2:product-list>"""

    seen_queries: list[dict[str, list[str]]] = []

    def meta_handler(req: httpx.Request) -> httpx.Response:
        assert req.url.host == "app.swup.update.sony.net"
        path = req.url.path
        if path == "/ess-distribution/public/api/product/v2":
            return httpx.Response(200, content=product_v2_xml)

        if path == "/ess-distribution/public/api/device-service/match/v2":
            qs = parse_qs(urlsplit(str(req.url)).query)
            seen_queries.append(qs)
            cdf = qs["CDFId"][0]
            if cdf == "28657952":
                assert qs.get("SonyProductCode") == ["43041672"]
                return httpx.Response(
                    200,
                    content=b"""<?xml version="1.0" encoding="UTF-8"?>
                    <match-response>
                      <device-problem>NO_PROBLEM</device-problem>
                      <device-service-infos>
                        <software-device-service-info>
                          <logic-type>CR_SOFTWARE_UPDATE</logic-type>
                          <software-version>73.0.A.2.53</software-version>
                          <anti-rollback-counters>
                            <anti-rollback-counter><key>vbmeta</key><value>1782864000</value></anti-rollback-counter>
                          </anti-rollback-counters>
                          <link rel="self" href="https://app.swup.update.sony.net/ess-distribution/public/api/device-service/software-service/gen2/ge74?password=secret123"/>
                        </software-device-service-info>
                      </device-service-infos>
                    </match-response>""",
                )
            if cdf == "1321-7192":
                assert "SonyProductCode" not in qs
                return httpx.Response(
                    200,
                    content=b"""<?xml version="1.0" encoding="UTF-8"?>
                    <match-response>
                      <device-problem>NO_PROBLEM</device-problem>
                      <device-service-infos>
                        <software-device-service-info>
                          <logic-type>SOFTWARE_UPDATE_CONTENT_ERASE</logic-type>
                          <software-version>58.2.A.7.93-R4C</software-version>
                          <anti-rollback-counters>
                            <anti-rollback-counter><key>vbmeta</key><value>1654041600</value></anti-rollback-counter>
                          </anti-rollback-counters>
                          <link rel="self" href="https://app.swup.update.sony.net/ess-distribution/public/api/device-service/software-service/at51?password=secret456"/>
                        </software-device-service-info>
                      </device-service-infos>
                    </match-response>""",
                )
            return httpx.Response(
                200,
                content=b"<match-response><device-problem>NO_SERVICE</device-problem><device-service-infos/></match-response>",
            )

        if path == "/ess-distribution/public/api/device-service/software-service/gen2/ge74":
            return httpx.Response(
                200,
                content=b"""<?xml version="1.0" encoding="UTF-8"?>
                <software-device-service>
                  <android-version>16</android-version>
                  <file-resources>
                    <file-resource>
                      <file-key>APP_SW</file-key>
                      <name>APP_SW_PDX267.zip</name>
                      <plain-size>120000000</plain-size>
                      <link rel="file-chunk-info" href="https://app.swup.update.sony.net/ess-distribution/public/api/file/101?password=abc"/>
                    </file-resource>
                  </file-resources>
                </software-device-service>""",
            )

        if path == "/ess-distribution/public/api/device-service/software-service/at51":
            return httpx.Response(
                200,
                content=b"""<?xml version="1.0" encoding="UTF-8"?>
                <software-device-service>
                  <file-resources>
                    <file-resource>
                      <file-key>APP_SW</file-key>
                      <name>APP_SW_PDX203.zip</name>
                      <plain-size>64000000</plain-size>
                      <link rel="file-chunk-info" href="https://app.swup.update.sony.net/ess-distribution/public/api/file/102?password=def"/>
                    </file-resource>
                  </file-resources>
                </software-device-service>""",
            )

        if path == "/ess-distribution/public/api/file/101":
            return httpx.Response(
                200,
                content=b"""<chunks>
                  <file-chunk number="1"><size>80000000</size><link rel="download" href="http://software.sonymobile.com/ns/part1.bin"/></file-chunk>
                  <file-chunk number="2"><size>40000000</size><link rel="download" href="http://software.sonymobile.com/ns/part2.bin"/></file-chunk>
                </chunks>""",
            )
        if path == "/ess-distribution/public/api/file/102":
            return httpx.Response(
                200,
                content=b"""<chunks>
                  <file-chunk number="0"><size>64000000</size><link rel="download" href="https://software.sony.net/ns/part0.bin"/></file-chunk>
                </chunks>""",
            )
        return httpx.Response(404)

    adapter = SonyAdapter(customizations_path=seed_path, request_delay_seconds=0.0)
    assert isinstance(get_adapter("sony"), SonyAdapter)

    with httpx.Client(transport=httpx.MockTransport(meta_handler)) as meta_client:
        summary = run_collector(
            "sony",
            state_dir=tmp_path,
            adapter=adapter,
            metadata_client=meta_client,
            resolve_dns=False,
        )

    assert summary["devices_discovered"] == 2
    assert summary["devices_checked"] == 2
    assert summary["releases_seen"] == 2
    assert summary["probed_verified"] == 2
    assert summary["probed_dead"] == 0
    assert summary["spooled_specs"] == 2

    outbox = OutboxSpool(tmp_path)
    specs = outbox.read_specs(outbox.list_pending()[0])
    by_code = {s.device.codename: s for s in specs}

    ge74 = by_code["sony-xq-ge74"]
    assert ge74.device.marketing_name == "Sony Xperia 1 VIII"
    assert ge74.device.boot_partition_type == "init_boot"
    assert ge74.release.package_type == "sony_bundle"
    assert ge74.release.delivery_mode == "r2_bundle"
    assert ge74.release.verification_status == "VERIFIED_R2_BUNDLE"
    assert ge74.release.android_version == "16"
    assert ge74.release.region_code == "Customized HK · 28657952"
    assert ge74.release.version_str == "73.0.A.2.53"
    assert ge74.enrichment is not None
    assert ge74.enrichment.security_patch == "2026-07-01"
    assert "password=" not in (ge74.enrichment.sources or {}).get("sony_ess", "")
    assert ge74.release.resolver_meta is not None
    ge74_key = manifest_digest(ge74.release.resolver_meta)
    assert ge74.release.fast_cdn_url == f"https://findmyrom.com/sony/bundles/{ge74_key}"
    # http://sonymobile.com normalized to https://
    assert ge74.release.resolver_meta["files"][0]["chunks"][0]["url"].startswith("https://software.sonymobile.com/")

    at51 = by_code["sony-xq-at51"]
    assert at51.device.marketing_name == "Sony Xperia 1 II"
    assert at51.device.boot_partition_type == "boot"
    assert at51.release.android_version == "unknown"
    assert at51.release.region_code == "Customized UK · 1321-7192"
    assert at51.enrichment is not None and at51.enrichment.security_patch == "2022-06-01"

    # Publish into local SQLite and verify load_bundle_record reads v4 firmware_delivery_specs
    db = D1Client(local_sqlite_path=str(tmp_path / "d1.sqlite"))
    db.execute_script(SCHEMA_SQL_PATH.read_text(encoding="utf-8"))
    ensure_schema(db, allow_migration=True, through=7)
    firmware_metadata_schema.ensure(db, apply=True)
    priv_key, _ = generate_keypair()
    pub_res = run_publisher(state_dir=tmp_path, db=db, private_key_hex=priv_key)
    assert pub_res["published_specs"] == 2
    assert db.query("SELECT COUNT(*) AS n FROM firmware_delivery_specs")[0]["n"] == 2

    loaded_record = load_bundle_record(db, ge74_key)
    assert loaded_record is not None
    assert loaded_record["manifest"] == ge74.release.resolver_meta
    assert loaded_record["public_token"].startswith("fw_")

    # Second run must reuse collector cache and spool 0 duplicates
    with httpx.Client(transport=httpx.MockTransport(meta_handler)) as meta_client:
        summary2 = run_collector(
            "sony",
            state_dir=tmp_path,
            adapter=adapter,
            metadata_client=meta_client,
            resolve_dns=False,
        )
    assert summary2["skipped_cached_probe"] == 2
    assert summary2["spooled_specs"] == 0
