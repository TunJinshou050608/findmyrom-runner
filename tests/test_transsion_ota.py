import gzip
import io
import json
import pytest
import httpx

from fmr_ingest_stock.transsion_ota import (
    encode_varint,
    decode_varint,
    encode_field,
    decode_fields,
    canonical_device_code,
    fetch_google_play_devices,
    export_device_catalogues,
    GoogleCheckinClient,
    TranssionDevice,
    TranssionFirmware,
    parse_transsion_version,
    normalize_transsion_firmware,
    probe_device_official_firmware,
    batch_probe_official_firmwares,
)


def test_protobuf_varint_roundtrip():
    test_values = [0, 1, 127, 128, 300, 16384, 1753951644, (1 << 32) - 1]
    for val in test_values:
        encoded = encode_varint(val)
        decoded, offset = decode_varint(encoded, 0)
        assert decoded == val
        assert offset == len(encoded)


def test_protobuf_field_encode_decode():
    # Test wire 0 (varint)
    f0 = encode_field(1, 0, encode_varint(42))
    decoded = decode_fields(f0)
    assert decoded == [(1, 0, 42)]

    # Test wire 2 (length-delimited string)
    payload = b"Hello Transsion"
    f2 = encode_field(3, 2, payload)
    decoded = decode_fields(f2)
    assert decoded == [(3, 2, payload)]


def test_canonical_device_code():
    assert canonical_device_code("tecno", "TECNO-CK7n") == "tecno-tecno-ck7n"
    assert canonical_device_code("infinix", "Infinix-X6833B") == "infinix-infinix-x6833b"
    assert canonical_device_code("itel", "itel-C671L") == "itel-itel-c671l"


def test_build_checkin_request():
    fp = "Infinix/X6528B-OP/Infinix-X6528B:13/TP1A.220624.014/250731V549:user/release-keys"
    compressed, headers = GoogleCheckinClient.build_checkin_request(
        fingerprint=fp,
        model="X6528B",
        timestamp=1753951644,
        serial="01234567",
        imei="860000000000001",
    )

    assert headers["content-type"] == "application/x-protobuffer"
    assert headers["content-encoding"] == "gzip"
    assert "Android 13" in headers["user-agent"]
    assert "X6528B" in headers["user-agent"]

    raw = gzip.decompress(compressed)
    fields = decode_fields(raw)
    field_tags = {f[0] for f in fields}
    # Verify required Google checkin request fields exist
    assert 1 in field_tags  # imei
    assert 4 in field_tags  # checkin proto
    assert 14 in field_tags # version
    assert 16 in field_tags # serialNumber
    assert 29 in field_tags # fetchSystemUpdates


def test_parse_checkin_response():
    # Construct a synthetic AndroidCheckinResponse with GservicesSettings
    setting1 = encode_field(1, 2, b"update_title") + encode_field(2, 2, b"Test_Update_V100")
    setting2 = encode_field(1, 2, b"update_url") + encode_field(2, 2, b"https://android.googleapis.com/packages/ota-api/package/test1234.zip")
    setting3 = encode_field(1, 2, b"update_size_value") + encode_field(2, 2, b"123456789")

    response_payload = (
        encode_field(5, 2, setting1) +
        encode_field(5, 2, setting2) +
        encode_field(5, 2, setting3)
    )

    parsed = GoogleCheckinClient.parse_checkin_response(response_payload)
    assert parsed.get("update_title") == "Test_Update_V100"
    assert parsed.get("update_url") == "https://android.googleapis.com/packages/ota-api/package/test1234.zip"
    assert parsed.get("update_size_value") == "123456789"


def test_probe_google_checkin_mock():
    fp = "Infinix/X6528B-OP/Infinix-X6528B:13/TP1A.220624.014/250731V549:user/release-keys"

    setting1 = encode_field(1, 2, b"update_title") + encode_field(2, 2, b"X6528B-OTA-Update")
    setting2 = encode_field(1, 2, b"update_url") + encode_field(2, 2, b"https://android.googleapis.com/packages/ota-api/package/mockhash.zip")
    setting3 = encode_field(1, 2, b"update_size_value") + encode_field(2, 2, b"94236056")
    setting4 = encode_field(1, 2, b"update_target_sdk_level") + encode_field(2, 2, b"33")

    fake_resp = (
        encode_field(5, 2, setting1) +
        encode_field(5, 2, setting2) +
        encode_field(5, 2, setting3) +
        encode_field(5, 2, setting4)
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://android.googleapis.com/checkin"
        assert request.headers["content-type"] == "application/x-protobuffer"
        return httpx.Response(200, content=fake_resp)

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as client:
        fw = GoogleCheckinClient.probe(fp, http_client=client)

    assert fw is not None
    assert fw.title == "X6528B-OTA-Update"
    assert fw.download_url == "https://android.googleapis.com/packages/ota-api/package/mockhash.zip"
    assert fw.size_bytes == 94236056
    assert fw.sdk_level == 33
    assert fw.brand == "infinix"
    assert fw.device_id == "Infinix-X6528B"


def test_fetch_google_play_devices_from_csv(tmp_path):
    csv_content = """Retail Branding,Marketing Name,Device,Model
TECNO,Camon 20 Pro 5G,TECNO-CK7n,CK7n
TECNO Mobile,POVA 5,TECNO-Mobile-LH7n,LH7n
Infinix,Zero 30 5G,Infinix-X6731B,X6731B
itel,Vision 3,itel-S661LN,S661LN
Samsung,Galaxy S24,SM-S921B,SM-S921B
"""
    sample_csv = tmp_path / "supported_devices_sample.csv"
    sample_csv.write_text(csv_content, encoding="utf-8")

    cats = fetch_google_play_devices(brands=("tecno", "infinix", "itel"), csv_path=sample_csv)
    assert len(cats["tecno"]) == 2
    assert len(cats["infinix"]) == 1
    assert len(cats["itel"]) == 1

    # Check mapping
    tecno_models = {d.model for d in cats["tecno"]}
    assert tecno_models == {"CK7n", "LH7n"}

    # Export test
    paths = export_device_catalogues(cats, tmp_path / "deliverables")
    assert paths["tecno_tsv"].exists()
    assert paths["combined_json"].exists()

    with open(paths["combined_json"], encoding="utf-8") as f:
        data = json.load(f)
    assert len(data) == 4


def test_parse_transsion_version_classic_and_modern():
    # Classic project format
    c1 = parse_transsion_version("Tcard_CK6n-H6929CD-U-OP-240713V809", brand="tecno")
    assert c1["naming_style"] == "classic_project"
    assert c1["model_code"] == "CK6n"
    assert c1["board_code"] == "H6929CD"
    assert c1["android_version"] == "14"
    assert c1["region_code"] == "OP"
    assert c1["build_number"] == "V809"
    assert c1["build_date"] == "2024-07-13"

    # Modern OS-versioned format
    m1 = parse_transsion_version("Tcard_X6852-16.3.0.130SP08-EU002PF001AZ_a15", brand="infinix")
    assert m1["naming_style"] == "modern_os"
    assert m1["model_code"] == "X6852"
    assert m1["os_name"] == "XOS"
    assert m1["region_code"] == "EU"
    assert m1["android_version"] == "15"
    assert "16.3.0.130" in m1["os_version"]

    # itel modern format
    it = parse_transsion_version("Tcard_S686LN-15.1.2.118SP01-OP001PF001AZ", brand="itel")
    assert it["naming_style"] == "modern_os"
    assert it["model_code"] == "S686LN"
    assert it["os_name"] == "itel OS"
    assert it["region_code"] == "OP"


def test_normalize_transsion_firmware_standard_fields():
    raw_sample = {
        "brand": "tecno",
        "device_id": "TECNO-CK6n",
        "title": "Tcard_CK6n-H6929CD-U-OP-240713V809",
        "download_url": "https://android.googleapis.com/packages/ota-api/package/4e8e2ff82e0be6393d60969f4063b60d4533481d.zip",
        "size_bytes": 6184179203,
        "marketing_name": "TECNO CAMON 20",
        "sdk_level": 34,
        "security_patch": "2024-06-05",
        "description": "Update Version: CK6n-H6929CD-U-OP-240713V809<br>Upgraded the HiOS version from HiOS 13.0.0 to HiOS 14.0.<br>",
    }

    norm = normalize_transsion_firmware(raw_sample)
    assert norm["brand_id"] == "tecno"
    assert norm["device_codename"] == "tecno-ck6n"
    assert norm["model_code"] == "CK6n"
    assert norm["marketing_name"] == "TECNO CAMON 20"
    assert norm["display_version"] == "CK6n-H6929CD-U-OP-240713V809"
    assert norm["region_code"] == "OP"
    assert norm["display_region"] == "Global (OP)"
    assert norm["android_version"] == "14"
    assert norm["os_name"] == "HiOS"
    assert norm["os_version"] == "14.0"
    assert norm["combined_system"] == "Android 14 · HiOS 14.0"
    assert norm["package_type"] == "recovery_zip"
    assert norm["file_size_str"] == "5.76 GB"
    assert norm["checksum_type"] == "sha1"
    assert norm["checksum"] == "4e8e2ff82e0be6393d60969f4063b60d4533481d"
    assert norm["published_date"] == "2024-07-13"


def test_probe_device_official_firmware_mock():
    # Mocking Google Checkin returning a full Tcard package
    setting1 = encode_field(1, 2, b"update_title") + encode_field(2, 2, b"Tcard_CK7n-H894ABC-U-GL-240703V1186")
    setting2 = encode_field(1, 2, b"update_url") + encode_field(2, 2, b"https://android.googleapis.com/packages/ota-api/package/6d9d6f44dc29143a77dc44e635a6a58343ee7dcc.zip")
    setting3 = encode_field(1, 2, b"update_size_value") + encode_field(2, 2, b"6754029100")
    setting4 = encode_field(1, 2, b"update_target_sdk_level") + encode_field(2, 2, b"34")

    fake_resp = (
        encode_field(5, 2, setting1) +
        encode_field(5, 2, setting2) +
        encode_field(5, 2, setting3) +
        encode_field(5, 2, setting4)
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://android.googleapis.com/checkin"
        return httpx.Response(200, content=fake_resp)

    dev = {
        "brand": "tecno",
        "model": "CK7n",
        "device": "TECNO-CK7n",
        "marketing_name": "CAMON 20 Pro 5G",
    }

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as client:
        fw = probe_device_official_firmware(dev, http_client=client)

    assert fw is not None
    assert fw.brand == "tecno"
    assert fw.device_id == "TECNO-CK7n"
    assert fw.title == "Tcard_CK7n-H894ABC-U-GL-240703V1186"
    assert fw.download_url == "https://android.googleapis.com/packages/ota-api/package/6d9d6f44dc29143a77dc44e635a6a58343ee7dcc.zip"
    assert not fw.is_incremental
    assert fw.size_bytes == 6754029100

    # Also test batch probe
    with httpx.Client(transport=transport) as client:
        fws = batch_probe_official_firmwares([dev], limit=1, http_client=client)
    assert len(fws) == 1
    assert fws[0].title == "Tcard_CK7n-H894ABC-U-GL-240703V1186"


