"""Offline unit & pipeline integration tests for the v4 TranssionAdapter (TECNO / Infinix / itel)."""

from __future__ import annotations

import gzip
from pathlib import Path
from typing import Any

import httpx
import pytest

from fmr_core.extraction_policy import extraction_eligible
from fmr_extractor.streaming_probe import RangeReader
from fmr_ingest_stock.adapters import get_adapter
from fmr_ingest_stock.adapters.base import OFFICIAL_METADATA_HOSTS, OfficialMetadataHTTP
from fmr_ingest_stock.adapters.transsion import TranssionAdapter
from fmr_ingest_stock.cdn_rewriter import validate_official_download_url
from fmr_ingest_stock.pipeline.collector import run_collector
from fmr_ingest_stock.pipeline.outbox import OutboxSpool
from fmr_ingest_stock.transsion_ota import decode_fields, encode_field


def _build_checkin_response_bytes(settings: dict[str, str]) -> bytes:
    out = bytearray()
    for k, v in settings.items():
        sub = encode_field(1, 2, k.encode("utf-8")) + encode_field(2, 2, v.encode("utf-8"))
        out.extend(encode_field(5, 2, sub))
    return bytes(out)


def _extract_checkin_fingerprint(req_content: bytes) -> str:
    raw = gzip.decompress(req_content)
    top_fields = dict((tag, val) for tag, _, val in decode_fields(raw))
    checkin_proto = dict((tag, val) for tag, _, val in decode_fields(top_fields[4]))
    build_proto = dict((tag, val) for tag, _, val in decode_fields(checkin_proto[1]))
    return build_proto[1].decode("utf-8")


def test_transsion_adapter_discovery_multi_region_android16_and_collector_outbox(
    tmp_path: Path,
) -> None:
    play_csv = (
        "Retail Branding,Marketing Name,Device,Model\n"
        "TECNO,CAMON 20 Pro,TECNO-CK7n,CK7n\n"
        "TECNO Mobile,PHANTOM Watch,TECNO-WATCH1,TW01\n"
        "TECNO,32X1,ikebukuro,AI PONT\n"
        "Infinix,GT 10 Pro,Infinix-X6739,X6739\n"
        "itel,itel A11,itel-A11,A11\n"
        "itel,Vision 3,itel-S661LN,S661LN\n"
    ).encode("utf-16")

    url_gl = "https://android.googleapis.com/packages/ota-api/package/6d9d6f44dc29143a77dc44e635a6a58343ee7dcc.zip"
    url_eu = "https://android.googleapis.com/packages/ota-api/package/1111111111111111111111111111111111111111.zip"
    url_delta = "https://android.googleapis.com/packages/ota-api/package/2222222222222222222222222222222222222222.zip"

    probed_fps: list[str] = []

    def meta_handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "storage.googleapis.com" and req.url.path == "/play_public/supported_devices.csv":
            return httpx.Response(200, content=play_csv)

        if req.url.host == "android.googleapis.com" and req.url.path == "/checkin":
            fp = _extract_checkin_fingerprint(req.content)
            probed_fps.append(fp)

            # Android 14 GL hit -> full Tcard classic package
            if "CK7n-GL/TECNO-CK7n:14/" in fp:
                return httpx.Response(
                    200,
                    content=_build_checkin_response_bytes(
                        {
                            "update_url": url_gl,
                            "update_title": "Tcard_CK7n-H894ABC-U-GL-240703V1186",
                            "update_size_value": "6753301047",
                            "update_target_sdk_level": "34",
                            "update_description": "Upgraded HiOS 13.0.0 to HiOS 14.0.",
                        }
                    ),
                )
            # Android 16 EU hit -> modern OS full Tcard package (verifies no early return!)
            if "CK7n-EU/TECNO-CK7n:16/" in fp:
                return httpx.Response(
                    200,
                    content=_build_checkin_response_bytes(
                        {
                            "update_url": url_eu,
                            "update_title": "Tcard_CK7n-16.1.0.120SP01-EU001PF001AZ_a16",
                            "update_size_value": "5900000000",
                            "update_target_sdk_level": "36",
                            "update_timestamp": "1770000000",
                        }
                    ),
                )
            # Incremental delta on IN -> MUST be filtered out
            if "CK7n-IN/TECNO-CK7n:14/" in fp:
                return httpx.Response(
                    200,
                    content=_build_checkin_response_bytes(
                        {
                            "update_url": url_delta,
                            "update_title": "update_CK7n_delta_ota",
                            "update_size_value": "320000000",
                            "update_target_sdk_level": "34",
                        }
                    ),
                )
            return httpx.Response(200, content=b"")

        return httpx.Response(404)

    def range_handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "android.googleapis.com":
            return httpx.Response(
                302,
                headers={"Location": f"https://redirector.gvt1.com{req.url.path}"},
            )
        if req.url.host == "redirector.gvt1.com":
            return httpx.Response(
                302,
                headers={"Location": f"https://rr1---sn-o097znzk.gvt1.com{req.url.path}?cms_redirect=yes"},
            )
        if req.url.host == "rr1---sn-o097znzk.gvt1.com":
            total = 6753301047 if "6d9d6f44" in req.url.path else 5900000000
            head = b"PK\x03\x04" + b"\x00" * 60
            return httpx.Response(
                206,
                headers={
                    "Content-Type": "application/octet-stream",
                    "Content-Range": f"bytes 0-63/{total}",
                    "ETag": "6d9d6f44dc29143a77dc44e635a6a58343ee7dcc",
                },
                content=head,
            )
        return httpx.Response(404)

    assert "android.googleapis.com" in OFFICIAL_METADATA_HOSTS
    assert validate_official_download_url(
        "tecno", "https://rr1---sn-o097znzk.gvt1.com/packages/data/ota-api/package/abc.zip", resolve_dns=False
    )

    with (
        httpx.Client(transport=httpx.MockTransport(meta_handler)) as meta_client,
        httpx.Client(transport=httpx.MockTransport(range_handler)) as range_client,
    ):
        adapter = TranssionAdapter("tecno")
        summary = run_collector(
            "tecno",
            state_dir=tmp_path,
            adapter=adapter,
            metadata_client=meta_client,
            range_client=range_client,
            resolve_dns=False,
        )

    assert summary["devices_discovered"] == 1
    assert summary["devices_checked"] == 1
    assert summary["releases_seen"] == 2
    assert summary["probed_verified"] == 2
    assert summary["probed_dead"] == 0
    assert summary["spooled_specs"] == 2
    assert any(":16/" in fp for fp in probed_fps)

    outbox = OutboxSpool(tmp_path)
    pending = outbox.list_pending()
    assert len(pending) == 1
    specs = outbox.read_specs(pending[0])
    assert len(specs) == 2

    by_region = {s.release.region_code: s for s in specs}
    assert set(by_region) == {"GL", "EU"}

    gl_spec = by_region["GL"]
    assert gl_spec.device.codename == "tecno-ck7n"
    assert gl_spec.device.marketing_name == "TECNO CAMON 20 Pro"
    assert gl_spec.device.brand_id == "tecno"
    assert gl_spec.device.boot_partition_type == "init_boot"
    assert gl_spec.release.package_type == "recovery_zip"
    assert gl_spec.release.delivery_mode == "direct_cdn"
    assert gl_spec.release.md5_or_sha256 is None
    assert gl_spec.enrichment is not None
    assert gl_spec.enrichment.os_name == "HiOS"
    assert gl_spec.enrichment.os_version == "14.0"
    assert gl_spec.enrichment.vendor_fields["ota_sha1"] == "6d9d6f44dc29143a77dc44e635a6a58343ee7dcc"
    assert extraction_eligible("tecno", gl_spec.release.package_type, gl_spec.release.fast_cdn_url) is True

    eu_spec = by_region["EU"]
    assert eu_spec.release.android_version == "16"
    assert eu_spec.release.published_date is not None
    assert extraction_eligible("tecno", eu_spec.release.package_type, eu_spec.release.fast_cdn_url) is True

    # 2nd collector pass must reuse cached probe and emit 0 duplicate Outbox specs
    with (
        httpx.Client(transport=httpx.MockTransport(meta_handler)) as meta_client,
        httpx.Client(transport=httpx.MockTransport(range_handler)) as range_client,
    ):
        summary2 = run_collector(
            "tecno",
            state_dir=tmp_path,
            adapter=TranssionAdapter("tecno"),
            metadata_client=meta_client,
            range_client=range_client,
            resolve_dns=False,
        )
    assert summary2["skipped_cached_probe"] == 2
    assert summary2["unchanged_not_spooled"] == 2
    assert summary2["spooled_specs"] == 0
    assert len(outbox.list_pending()) == 1

    # Verify itel A11 fallback keeps model name rather than collapsing to bare "itel"
    with httpx.Client(transport=httpx.MockTransport(meta_handler)) as meta_client:
        itel_devs = TranssionAdapter("itel").discover_devices(OfficialMetadataHTTP(client=meta_client))
    itel_names = {d.codename: d.marketing_name for d in itel_devs}
    assert itel_names["itel-a11"] == "itel A11"
    assert itel_names["itel-s661ln"] == "itel Vision 3"


def test_transsion_adapter_pacing_and_429_surface_to_collector(tmp_path: Path) -> None:
    play_csv = (
        "Retail Branding,Marketing Name,Device,Model\n"
        "Infinix,GT 10 Pro,Infinix-X6739,X6739\n"
    ).encode("utf-8")

    sleeps: list[float] = []
    calls = {"checkin": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "storage.googleapis.com":
            return httpx.Response(200, content=play_csv)
        calls["checkin"] += 1
        return httpx.Response(429, headers={"Retry-After": "1"})

    adapter = TranssionAdapter(
        "infinix",
        build_seeds=((15, "AP3A.240905.015.A2"),),
        regions=("OP", "GL"),
        request_delay_seconds=0.1,
        sleep_fn=sleeps.append,
    )

    with httpx.Client(transport=httpx.MockTransport(handler)) as meta_client:
        http = OfficialMetadataHTTP(
            client=meta_client,
            max_retries=1,
            sleep_fn=lambda _: None,
        )
        devices = adapter.discover_devices(http)
        assert len(devices) == 1
        assert devices[0].codename == "infinix-x6739"
        with pytest.raises(httpx.HTTPStatusError):
            adapter.fetch_releases(devices[0], http)

    # Stopped immediately after first 429 exhausted OfficialMetadataHTTP retries (2 attempts)
    assert calls["checkin"] == 2
    assert sleeps == []

    # Verify adapter registration for all 3 brands
    for vid in ("tecno", "infinix", "itel"):
        inst = get_adapter(vid)
        assert isinstance(inst, TranssionAdapter)
        assert inst.vendor_id == vid


def test_transsion_probe_cap_and_streaming_probe_gvt1_redirect() -> None:
    calls = {"checkin": 0}

    def meta_handler(req: httpx.Request) -> httpx.Response:
        calls["checkin"] += 1
        return httpx.Response(200, content=b"")

    sleeps: list[float] = []
    adapter = TranssionAdapter(
        "itel",
        max_probes_per_device=3,
        request_delay_seconds=0.05,
        sleep_fn=sleeps.append,
    )
    with httpx.Client(transport=httpx.MockTransport(meta_handler)) as meta_client:
        http = OfficialMetadataHTTP(client=meta_client, sleep_fn=lambda _: None)
        from fmr_ingest_stock.adapters.base import AdapterDevice

        dev = AdapterDevice(
            codename="itel-s661ln",
            brand_id="itel",
            marketing_name="itel Vision 3",
            model_numbers=("S661LN",),
            vendor_meta={"models": ["S661LN"], "raw_codenames": ["itel-S661LN"]},
        )
        assert adapter.fetch_releases(dev, http) == []

    assert calls["checkin"] == 3
    assert sleeps == [0.05, 0.05]

    # Verify streaming_probe.RangeReader handles googleapis -> gvt1 302 redirects cleanly
    payload = b"PK\x03\x04" + b"A" * 100
    def stream_handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "android.googleapis.com":
            return httpx.Response(302, headers={"Location": "https://redirector.gvt1.com/packages/data/ota-api/package/x.zip"})
        if req.url.host == "redirector.gvt1.com":
            range_hdr = req.headers.get("range", "bytes=0-0")
            bounds = range_hdr.removeprefix("bytes=").split("-")
            start, end = int(bounds[0]), int(bounds[1])
            return httpx.Response(
                206,
                headers={"Content-Range": f"bytes {start}-{end}/{len(payload)}"},
                stream=httpx.ByteStream(payload[start : end + 1]),
            )
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(stream_handler)) as c:
        rr = RangeReader("https://android.googleapis.com/packages/ota-api/package/x.zip", c)
        assert rr.size == len(payload)
        assert rr.url.startswith("https://redirector.gvt1.com/")


def test_transsion_data_cleaning_dedup_crossbrand_and_sdk36_priority() -> None:
    play_csv = (
        "Retail Branding,Marketing Name,Device,Model\n"
        "Tecno,TECNO SPARK 20C,TECNO-BG7n,BG7n\n"
        "Tecno Mobile,TECNO Mobile SPARK 20C,TECNO-Mobile-BG7n,TECNO Mobile BG7n\n"
        "Tecno,Infinix SMART 7,Infinix-X6515,Infinix X6515\n"
        "Infinix,Infinix SMART 7,Infinix-X6515,Infinix X6515\n"
        "Itel,itel-A669L,itel-A669L,itel A669L\n"
        "Itel,itel A50C,itel-A669L,itel A669L\n"
        "Itel,ITELL_K4700Q,ITELL_K4700Q,ITELL K4700Q\n"
    ).encode("utf-8")

    cl6_url = "https://android.googleapis.com/packages/ota-api/package/ce4d153602406ab2b655f4d022dbabab66ab025d.zip"

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "storage.googleapis.com":
            return httpx.Response(200, content=play_csv)
        if req.url.host == "android.googleapis.com" and req.url.path == "/checkin":
            fp = _extract_checkin_fingerprint(req.content)
            # Simulate modern `_a15` filename carrying Android 16 (SDK 36), matching live CL6/X6876
            if "BG7n-OP/" in fp:
                return httpx.Response(
                    200,
                    content=_build_checkin_response_bytes(
                        {
                            "update_url": cl6_url,
                            "update_title": "Tcard_BG7n-16.3.0.145SP09-OP005PF001AZ_a15",
                            "update_size_value": "7696965553",
                            "update_target_sdk_level": "36",
                        }
                    ),
                )
            return httpx.Response(200, content=b"")
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        http = OfficialMetadataHTTP(client=client, sleep_fn=lambda _: None)

        # 1. TECNO merges TECNO-BG7n & TECNO-Mobile-BG7n and skips Infinix-X6515 cross-brand typo
        tecno = TranssionAdapter(
            "tecno",
            build_seeds=((16, "BP2A.250605.031.A2"),),
            regions=("OP",),
            request_delay_seconds=0.0,
        )
        tecno_devs = tecno.discover_devices(http)
        assert [d.codename for d in tecno_devs] == ["tecno-bg7n"]
        bg7n = tecno_devs[0]
        assert bg7n.marketing_name == "TECNO SPARK 20C"
        assert bg7n.model_numbers == ("BG7n",)
        assert bg7n.vendor_meta["raw_codenames"] == ["TECNO-BG7n", "TECNO-Mobile-BG7n"]

        # 2. Modern `_a15` upgrade package with update_target_sdk_level=36 must map to Android 16 (not 15)
        rels = tecno.fetch_releases(bg7n, http)
        assert len(rels) == 1
        assert rels[0].android_version == "16"
        assert rels[0].os_name == "HiOS"
        assert rels[0].os_version == "16.3.0.145 SP09"
        assert rels[0].region_code == "OP"
        assert rels[0].model_code == "BG7n"

        # 3. Infinix keeps Infinix-X6515 cleanly
        inf = TranssionAdapter("infinix")
        inf_devs = inf.discover_devices(http)
        assert [(d.codename, d.marketing_name) for d in inf_devs] == [("infinix-x6515", "Infinix SMART 7")]

        # 4. itel picks retail name ("itel A50C") even when raw codename ("itel-A669L") came first, and cleans `_`
        itel = TranssionAdapter("itel")
        itel_devs = {d.codename: d.marketing_name for d in itel.discover_devices(http)}
        assert itel_devs["itel-a669l"] == "itel A50C"
        assert "_" not in itel_devs["itel-itell-k4700q"]
        assert itel_devs["itel-itell-k4700q"] == "itel ITELL K4700Q"


def test_transsion_collect_priority_and_brand_sort_order(tmp_path: Path) -> None:
    from fmr_core.d1_client import D1Client
    from fmr_ingest_stock.pipeline.publisher import (
        DEFAULT_BRAND_SORT_ORDERS,
        run_publisher,
    )

    play_csv = (
        "Retail Branding,Marketing Name,Device,Model\n"
        "itel,itel A11,itel-A11,A11\n"
        "itel,itel S25 Ultra,itel-S686LN,S686LN\n"
    ).encode("utf-8")

    s25_url = "https://android.googleapis.com/packages/ota-api/package/da4474145e5f5e00000000000000000000000000.zip"
    probed_codenames: list[str] = []

    def meta_handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "storage.googleapis.com":
            return httpx.Response(200, content=play_csv)
        if req.url.host == "android.googleapis.com" and req.url.path == "/checkin":
            fp = _extract_checkin_fingerprint(req.content)
            probed_codenames.append(fp.split("/")[1])
            if "S686LN-OP/" in fp and ":15/" in fp:
                return httpx.Response(
                    200,
                    content=_build_checkin_response_bytes(
                        {
                            "update_url": s25_url,
                            "update_title": "Tcard_S686LN-15.1.2.118SP01-OP001PF001AZ",
                            "update_size_value": "6180000000",
                            "update_target_sdk_level": "35",
                        }
                    ),
                )
            return httpx.Response(200, content=b"")
        return httpx.Response(404)

    def range_handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            206,
            headers={"Content-Range": "bytes 0-63/6180000000", "ETag": '"etag1"'},
            content=b"PK\x03\x04" + b"\x00" * 60,
        )

    state_dir = tmp_path / "state"
    with (
        httpx.Client(transport=httpx.MockTransport(meta_handler)) as meta_client,
        httpx.Client(transport=httpx.MockTransport(range_handler)) as range_client,
    ):
        summary = run_collector(
            "itel",
            state_dir=state_dir,
            adapter=TranssionAdapter("itel", regions=("OP",), build_seeds=((15, "AP3A.240905.015.A2"),)),
            metadata_client=meta_client,
            range_client=range_client,
            max_devices=1,
            resolve_dns=False,
        )

    assert summary["devices_discovered"] == 2
    assert summary["devices_checked"] == 1
    assert summary["spooled_specs"] == 1
    # S25 Ultra (collect_priority=10) MUST be checked before legacy itel A11 (collect_priority=900)
    assert probed_codenames == ["S686LN-OP"]

    # Publishing itel inserts brands row with sort_order before 'other' (99)
    assert DEFAULT_BRAND_SORT_ORDERS["tecno"] < DEFAULT_BRAND_SORT_ORDERS["other"]
    assert DEFAULT_BRAND_SORT_ORDERS["infinix"] < DEFAULT_BRAND_SORT_ORDERS["other"]
    assert DEFAULT_BRAND_SORT_ORDERS["itel"] < DEFAULT_BRAND_SORT_ORDERS["other"]

    db = D1Client(local_sqlite_path=str(tmp_path / "d1.sqlite"))
    try:
        schema_sql = (Path(__file__).resolve().parents[1] / "fmr_core" / "schema.sql").read_text(encoding="utf-8")
        db.execute_script(schema_sql)
        res = run_publisher(
            state_dir=state_dir,
            db=db,
            private_key_hex="11" * 32,
            brand_names={"itel": "itel"},
        )
        assert res["status"] == "ok"
        assert res["published_specs"] == 1
        brand_row = db.query("SELECT id, name, sort_order FROM brands WHERE id = 'itel'")[0]
        assert brand_row["name"] == "itel"
        assert brand_row["sort_order"] == DEFAULT_BRAND_SORT_ORDERS["itel"]
    finally:
        db.close()
