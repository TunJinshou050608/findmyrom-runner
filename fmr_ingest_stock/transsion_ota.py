"""Transsion (TECNO / Infinix / itel) official firmware & device catalogue client.

Metadata and link discovery only; NEVER fetches firmware binary bodies.
Provides:
1. Official Google Play certified device catalogue parsing for all Transsion brands.
2. Pure-Python protobuf serialization and deserialization for Google Android GOTA checkin.
3. Direct live query to Google's official checkin server (`android.googleapis.com/checkin`)
   to obtain official firmware update download links (`android.googleapis.com/packages/ota-api/...`).
4. High-performance batch retrieval of indexed official FULL ROM packages (Tcard 全量包).
5. Strict exclusion of delta/incremental OTA packages by default.
6. Fully automated CLI and programmatic interface conforming to FindMyROM runner standards.
"""
from __future__ import annotations

import argparse
import binascii
import csv
from dataclasses import dataclass, asdict
import gzip
import io
import json
import os
from pathlib import Path
import random
import re
import time
from typing import Any, Iterable
from urllib.parse import urlsplit

import httpx

MAX_METADATA_BYTES = 16 * 1024 * 1024
GOOGLE_SUPPORTED_DEVICES_URL = 'https://storage.googleapis.com/play_public/supported_devices.csv'
GOOGLE_CHECKIN_URL = 'https://android.googleapis.com/checkin'

TRANSSION_BRANDS: dict[str, str] = {
    'tecno': 'TECNO',
    'tecno mobile': 'TECNO',
    'infinix': 'Infinix',
    'itel': 'itel',
}


# ==============================================================================
# Pure-Python Protobuf Varint & Wire Format Utilities
# ==============================================================================

def encode_varint(value: int) -> bytes:
    """Encode an integer as a standard protocol buffer varint."""
    if value < 0:
        value = (1 << 64) + value
    bits = value & 0x7F
    value >>= 7
    res = bytearray()
    while value:
        res.append(0x80 | bits)
        bits = value & 0x7F
        value >>= 7
    res.append(bits)
    return bytes(res)


def decode_varint(data: bytes, offset: int = 0) -> tuple[int, int]:
    """Decode a standard protocol buffer varint from data starting at offset."""
    res = 0
    shift = 0
    while True:
        if offset >= len(data):
            raise ValueError('Varint offset out of bounds')
        b = data[offset]
        offset += 1
        res |= (b & 0x7F) << shift
        if not (b & 0x80):
            break
        shift += 7
    return res, offset


def encode_field(tag: int, wire: int, data: bytes) -> bytes:
    """Encode a single field header and its payload."""
    header = encode_varint((tag << 3) | wire)
    if wire == 0:
        return header + data
    elif wire == 2:
        return header + encode_varint(len(data)) + data
    raise ValueError(f'Unsupported wire type: {wire}')


def decode_fields(data: bytes) -> list[tuple[int, int, bytes | int]]:
    """Decode top-level fields into (tag, wire_type, value_or_bytes)."""
    offset = 0
    fields = []
    while offset < len(data):
        tag_wire, offset = decode_varint(data, offset)
        tag = tag_wire >> 3
        wire = tag_wire & 7
        if wire == 0:
            val, offset = decode_varint(data, offset)
            fields.append((tag, wire, val))
        elif wire == 2:
            length, offset = decode_varint(data, offset)
            chunk = data[offset:offset + length]
            offset += length
            fields.append((tag, wire, chunk))
        elif wire == 5:
            offset += 4
            fields.append((tag, wire, b''))
        elif wire == 1:
            offset += 8
            fields.append((tag, wire, b''))
        else:
            break
    return fields


# ==============================================================================
# Data Structures
# ==============================================================================

@dataclass
class TranssionDevice:
    brand: str
    retail_branding: str
    marketing_name: str
    device: str
    model: str
    canonical_code: str

    def to_tsv_row(self) -> str:
        return f"{self.model}\t{self.device}\t{self.marketing_name}"


@dataclass
class TranssionFirmware:
    brand: str
    device_id: str
    version: str
    title: str
    download_url: str
    size_bytes: int
    is_incremental: bool
    marketing_name: str | None = None
    sdk_level: int | None = None
    security_patch: str | None = None
    precondition_fingerprint: str | None = None
    postcondition_fingerprint: str | None = None
    build_timestamp: int | None = None
    description: str | None = None
    origin_kind: str = 'oem'
    metadata_source: str = 'google_ota_api'


# ==============================================================================
# Goal 1: Reliable Device / Model Catalog Extraction
# ==============================================================================

def canonical_device_code(brand: str, device: str) -> str:
    """Generate a clean, deterministic canonical identifier."""
    raw = f"{brand}-{device}".lower()
    cleaned = re.sub(r'[^a-z0-9-]+', '-', raw).strip('-')
    return cleaned[:32]


def fetch_google_play_devices(
    brands: Iterable[str] = ('tecno', 'infinix', 'itel'),
    http_client: httpx.Client | None = None,
    csv_path: Path | str | None = None,
) -> dict[str, list[TranssionDevice]]:
    """Fetch and parse Google Play's official supported_devices.csv for Transsion brands.
    
    If `csv_path` is supplied and exists, reads from local file instead of network.
    """
    wanted = {b.strip().lower() for b in brands}
    results: dict[str, list[TranssionDevice]] = {b: [] for b in ('tecno', 'infinix', 'itel') if b in wanted}
    seen: set[tuple[str, str, str]] = set()

    text = ""
    if csv_path and Path(csv_path).exists():
        raw_bytes = Path(csv_path).read_bytes()
        text = raw_bytes.decode('utf-16') if raw_bytes[:2] in (b'\xff\xfe', b'\xfe\xff') else raw_bytes.decode('utf-8-sig')
    else:
        owned = http_client is None
        client = http_client or httpx.Client(timeout=30, follow_redirects=True)
        try:
            with client.stream('GET', GOOGLE_SUPPORTED_DEVICES_URL) as resp:
                resp.raise_for_status()
                data = bytearray()
                for chunk in resp.iter_bytes(65536):
                    data.extend(chunk)
                    if len(data) > MAX_METADATA_BYTES:
                        raise ValueError('Google Play CSV exceeded size bound')
                raw_bytes = bytes(data)
                text = raw_bytes.decode('utf-16') if raw_bytes[:2] in (b'\xff\xfe', b'\xfe\xff') else raw_bytes.decode('utf-8-sig')
        finally:
            if owned:
                client.close()

    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        raw_brand = (row.get('Retail Branding') or '').strip().lower()
        normalized_brand = TRANSSION_BRANDS.get(raw_brand)
        if not normalized_brand:
            continue
        brand_key = normalized_brand.lower()
        if brand_key not in wanted:
            continue

        marketing = (row.get('Marketing Name') or '').strip()
        device = (row.get('Device') or '').strip()
        model = (row.get('Model') or '').strip()
        if not device and not model:
            continue

        dedup_key = (brand_key, model, device)
        if dedup_key in seen:
            continue
        seen.add(dedup_key)

        entry = TranssionDevice(
            brand=brand_key,
            retail_branding=normalized_brand,
            marketing_name=marketing or model or device,
            device=device,
            model=model or device,
            canonical_code=canonical_device_code(brand_key, device or model),
        )
        results.setdefault(brand_key, []).append(entry)

    for brand_key in results:
        results[brand_key].sort(key=lambda d: (d.marketing_name.lower(), d.model.lower()))

    return results


def export_device_catalogues(
    catalogues: dict[str, list[TranssionDevice]],
    output_dir: Path | str,
) -> dict[str, Path]:
    """Export device models to TSV (.txt) and comprehensive JSON files."""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}

    all_devices: list[TranssionDevice] = []
    for brand, devices in catalogues.items():
        all_devices.extend(devices)
        tsv_path = out / f"{brand}_models.txt"
        with open(tsv_path, 'w', encoding='utf-8') as f:
            for d in devices:
                f.write(d.to_tsv_row() + '\n')
        paths[f"{brand}_tsv"] = tsv_path

    combined_tsv = out / "transsion_models.txt"
    all_devices.sort(key=lambda d: (d.brand, d.marketing_name.lower(), d.model.lower()))
    with open(combined_tsv, 'w', encoding='utf-8') as f:
        for d in all_devices:
            f.write(f"{d.brand}\t{d.to_tsv_row()}\n")
    paths["combined_tsv"] = combined_tsv

    json_path = out / "transsion_devices.json"
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump([asdict(d) for d in all_devices], f, indent=2, ensure_ascii=False)
    paths["combined_json"] = json_path

    return paths


# ==============================================================================
# Goal 2: Direct Google Android Checkin Protocol (Pure Python)
# ==============================================================================

class GoogleCheckinClient:
    """Pure-Python client for Google's Android Over-The-Air checkin endpoint."""

    @staticmethod
    def build_checkin_request(
        fingerprint: str,
        model: str | None = None,
        timestamp: int = 0,
        serial: str | None = None,
        imei: str | None = None,
    ) -> tuple[bytes, dict[str, str]]:
        """Serialize AndroidCheckinRequest into gzip-compressed protobuf bytes."""
        parts = fingerprint.split('/')
        if len(parts) < 5 or ':' not in parts[2]:
            raise ValueError(f"Invalid Android fingerprint format: {fingerprint}")

        dev_ver = parts[2].split(':')
        device = dev_ver[0]
        android_version = dev_ver[1]
        build_id = parts[4]
        if not model:
            model = device

        if not imei:
            imei = ''.join([str(random.randint(0, 9)) for _ in range(15)])
        if not serial:
            serial = ''.join([random.choice('0123456789abcdef') for _ in range(8)])

        digest = '1-' + ''.join([random.choice('0123456789abcdef') for _ in range(40)])
        mac = binascii.b2a_hex(os.urandom(6))

        # 1. AndroidBuildProto
        build_msg = (
            encode_field(1, 2, fingerprint.encode()) +
            encode_field(7, 0, encode_varint(timestamp)) +
            encode_field(9, 2, device.encode())
        )

        # 2. AndroidCheckinProto
        checkin_proto = (
            encode_field(1, 2, build_msg) +
            encode_field(2, 0, encode_varint(0)) +
            encode_field(8, 2, b'WIFI::') +
            encode_field(9, 0, encode_varint(0)) +
            encode_field(14, 0, encode_varint(2)) +
            encode_field(18, 0, encode_varint(0)) +
            encode_field(19, 2, b'WIFI')
        )

        # 3. AndroidCheckinRequest
        req_msg = (
            encode_field(1, 2, imei.encode()) +
            encode_field(2, 0, encode_varint(0)) +
            encode_field(3, 2, digest.encode()) +
            encode_field(4, 2, checkin_proto) +
            encode_field(6, 2, b'en-US') +
            encode_field(9, 2, mac) +
            encode_field(12, 2, b'America/New_York') +
            encode_field(14, 0, encode_varint(3)) +
            encode_field(16, 2, serial.encode()) +
            encode_field(19, 2, b'wifi') +
            encode_field(20, 0, encode_varint(0)) +
            encode_field(22, 0, encode_varint(0)) +
            encode_field(29, 0, encode_varint(1)) +  # fetchSystemUpdates
            encode_field(30, 0, encode_varint(0))
        )

        compressed = gzip.compress(req_msg)
        headers = {
            'accept-encoding': 'gzip, deflate',
            'content-encoding': 'gzip',
            'content-type': 'application/x-protobuffer',
            'user-agent': f'Dalvik/2.1.0 (Linux; U; Android {android_version}; {model} Build/{build_id})',
        }
        return compressed, headers

    @staticmethod
    def parse_checkin_response(data: bytes) -> dict[str, str]:
        """Decode GservicesSettings from AndroidCheckinResponse payload."""
        settings: dict[str, str] = {}
        for tag, wire, val in decode_fields(data):
            if tag == 5 and isinstance(val, bytes):  # GservicesSetting
                sub_fields = decode_fields(val)
                s_name = None
                s_val = None
                for s_tag, s_wire, s_data in sub_fields:
                    if s_tag == 1 and isinstance(s_data, bytes):
                        s_name = s_data
                    elif s_tag == 2 and isinstance(s_data, bytes):
                        s_val = s_data
                if s_name:
                    k = s_name.decode(errors='ignore')
                    v = s_val.decode(errors='ignore') if s_val else ''
                    settings[k] = v
        return settings

    @classmethod
    def probe(
        cls,
        fingerprint: str,
        model: str | None = None,
        timestamp: int = 0,
        http_client: httpx.Client | None = None,
    ) -> TranssionFirmware | None:
        """Query Google's official checkin server for updates on this fingerprint.
        
        Strictly retrieves metadata and download URL. NEVER downloads firmware binary.
        """
        payload, headers = cls.build_checkin_request(fingerprint, model=model, timestamp=timestamp)
        owned = http_client is None
        client = http_client or httpx.Client(timeout=20, follow_redirects=False)
        try:
            resp = client.post(GOOGLE_CHECKIN_URL, content=payload, headers=headers)
            resp.raise_for_status()
            settings = cls.parse_checkin_response(resp.content)
            download_url = settings.get('update_url')
            if not download_url:
                return None

            # Verify security of download URL
            p = urlsplit(download_url)
            if p.hostname != 'android.googleapis.com' or not p.path.startswith('/packages/ota-api/'):
                raise ValueError(f"Unexpected non-OEM download URL: {download_url}")

            brand = fingerprint.split('/')[0].lower()
            device = fingerprint.split('/')[2].split(':')[0]
            title = settings.get('update_title') or download_url.split('/')[-1]
            size_val = int(settings.get('update_size_value') or 0)
            target_sdk = int(settings.get('update_target_sdk_level')) if settings.get('update_target_sdk_level') else None

            is_incremental = not (title.startswith('Tcard_') or size_val > 1024 * 1024 * 1024)

            return TranssionFirmware(
                brand=brand,
                device_id=device,
                version=title,
                title=title,
                download_url=download_url,
                size_bytes=size_val,
                is_incremental=is_incremental,
                sdk_level=target_sdk,
                description=settings.get('update_description'),
                precondition_fingerprint=fingerprint,
                metadata_source=GOOGLE_CHECKIN_URL,
            )
        finally:
            if owned:
                client.close()


# ==============================================================================
# Goal 2: Batch Indexed Firmware Retrieval (Full Packages)
# ==============================================================================

def load_device_marketing_map(deliverables_dir: Path | str | None = None) -> dict[str, str]:
    """Map device codename -> marketing name if transsion_devices.json exists."""
    mapping: dict[str, str] = {}
    if deliverables_dir:
        json_path = Path(deliverables_dir) / 'transsion_devices.json'
        if json_path.exists():
            try:
                data = json.loads(json_path.read_text(encoding='utf-8'))
                for entry in data:
                    dev = (entry.get('device') or '').strip().lower()
                    mkt = (entry.get('marketing_name') or '').strip()
                    if dev and mkt:
                        mapping[dev] = mkt
            except Exception:
                pass
    return mapping


def probe_device_official_firmware(
    device: TranssionDevice | dict[str, Any],
    http_client: httpx.Client | None = None,
) -> TranssionFirmware | None:
    """Probe Google GOTA official server directly for a Transsion device."""
    if isinstance(device, TranssionDevice):
        d_brand = device.brand.lower()
        d_model = device.model
        d_code = device.device
        d_marketing = device.marketing_name
    else:
        d_brand = device.get('brand', '').lower()
        d_model = device.get('model', '')
        d_code = device.get('device', '')
        d_marketing = device.get('marketing_name', '')

    # Strip brand prefix if present in model (e.g. 'Infinix X6871' -> 'X6871')
    d_model = re.sub(r'^(?:TECNO|Infinix|itel|TECNO\s*Mobile)\s*', '', d_model, flags=re.I).strip()
    # Strip brand prefix if present in device code (e.g. 'Infinix-X6871' -> 'Infinix-X6871', keep canonical)
    b_title = 'TECNO' if d_brand == 'tecno' else ('Infinix' if d_brand == 'infinix' else 'Itel')

    # Candidate release builds and regions for official Transsion devices
    probes = [
        (14, 'UP1A.231005.007', 'OP'),
        (14, 'UP1A.231005.007', 'GL'),
        (15, 'AP3A.240905.015.A2', 'OP'),
        (15, 'AP3A.240905.015.A2', 'GL'),
        (13, 'TP1A.220624.014', 'GL'),
        (13, 'TP1A.220624.014', 'OP'),
        (12, 'SP1A.210812.016', 'GL'),
        (12, 'SP1A.210812.016', 'OP'),
        (11, 'RP1A.200720.011', 'GL'),
        (11, 'RP1A.200720.011', 'OP'),
        (14, 'UP1A.231005.007', 'RU'),
        (14, 'UP1A.231005.007', 'IN'),
        (13, 'TP1A.220624.014', 'RU'),
        (13, 'TP1A.220624.014', 'IN'),
        (15, 'AP3A.240905.015.A2', 'RU'),
        (15, 'AP3A.240905.015.A2', 'IN'),
    ]

    for a_ver, b_id, reg in probes:
        fp = f"{b_title}/{d_model}-{reg}/{d_code}:{a_ver}/{b_id}/1:user/release-keys"
        try:
            fw = GoogleCheckinClient.probe(fp, model=d_model, http_client=http_client)
            if fw and not fw.is_incremental and fw.size_bytes > 500 * 1024 * 1024:
                fw.brand = d_brand
                fw.device_id = d_code
                fw.marketing_name = d_marketing
                return fw
        except Exception:
            continue

    return None


def batch_probe_official_firmwares(
    devices: list[dict[str, Any] | TranssionDevice],
    limit: int = 50,
    brand_filter: str | None = None,
    http_client: httpx.Client | None = None,
) -> list[TranssionFirmware]:
    """Batch probe Google official checkin server directly across Transsion devices."""
    owned = http_client is None
    client = http_client or httpx.Client(timeout=15, headers={'User-Agent': 'Dalvik/2.1.0'})
    firmwares: list[TranssionFirmware] = []
    seen_urls: set[str] = set()

    try:
        def is_phone(d: dict[str, Any] | TranssionDevice) -> bool:
            mkt = (d.marketing_name if isinstance(d, TranssionDevice) else d.get('marketing_name', '')).lower()
            return not any(x in mkt for x in ['tv', 'box', 'watch'])

        def sort_priority(d: dict[str, Any] | TranssionDevice) -> int:
            mkt = (d.marketing_name if isinstance(d, TranssionDevice) else d.get('marketing_name', '')).upper()
            high_pri = [
                'GT 10', 'GT 20', 'GT 30', 'NOTE 30', 'NOTE 40', 'ZERO 30', 'ZERO 40',
                'CAMON 20', 'CAMON 30', 'PHANTOM', 'POVA 5', 'POVA 6', 'SPARK 20',
                'S24', 'S25', 'P55', 'COLORPRO'
            ]
            for idx, k in enumerate(high_pri):
                if k in mkt:
                    return idx
            return 999

        if not brand_filter or brand_filter == 'all':
            # Interleave across tecno, infinix, itel
            by_brand: dict[str, list[Any]] = {'tecno': [], 'infinix': [], 'itel': []}
            for d in devices:
                b = (d.brand if isinstance(d, TranssionDevice) else d.get('brand', '')).lower()
                if b in by_brand and is_phone(d):
                    by_brand[b].append(d)
            for b in by_brand:
                by_brand[b].sort(key=sort_priority)

            # Round robin
            filtered = []
            max_len = max(len(lst) for lst in by_brand.values()) if any(by_brand.values()) else 0
            for i in range(max_len):
                for b in ('tecno', 'infinix', 'itel'):
                    if i < len(by_brand[b]):
                        filtered.append(by_brand[b][i])
        else:
            filtered = [
                d for d in devices
                if (d.brand if isinstance(d, TranssionDevice) else d.get('brand', '')).lower() == brand_filter.lower() and is_phone(d)
            ]
            filtered.sort(key=sort_priority)

        for dev in filtered:
            if limit and len(firmwares) >= limit:
                break
            fw = probe_device_official_firmware(dev, http_client=client)
            if fw and fw.download_url not in seen_urls:
                seen_urls.add(fw.download_url)
                firmwares.append(fw)
                m_name = dev.marketing_name if isinstance(dev, TranssionDevice) else dev.get('marketing_name', '')
                d_code = dev.device if isinstance(dev, TranssionDevice) else dev.get('device', '')
                print(f"  [OFFICIAL SERVER HIT] {fw.brand.upper():8s} {d_code:16s} ({m_name}): {fw.title} ({fw.size_bytes/(1024**3):.2f} GB)")
    finally:
        if owned:
            client.close()

    return firmwares


# ==============================================================================
# CLI Entry Point
# ==============================================================================

# Standard Transsion Region and Brand OS mappings
TRANSSION_REGIONS: dict[str, str] = {
    'OP': 'Global (OP)',
    'GL': 'Global',
    'EU': 'Europe · EU',
    'EEA': 'Europe · EEA',
    'IN': 'India · IN',
    'RU': 'Russia · RU',
    'ID': 'Indonesia · ID',
    'TR': 'Türkiye · TR',
    'MEA': 'Middle East & Africa',
    'AF': 'Africa',
    'LATAM': 'Latin America',
}

LETTER_TO_ANDROID: dict[str, str] = {
    'P': '9', 'Q': '10', 'R': '11', 'S': '12', 'T': '13', 'U': '14', 'V': '15', 'W': '16'
}

BRAND_TO_OS: dict[str, str] = {
    'tecno': 'HiOS',
    'infinix': 'XOS',
    'itel': 'itel OS'
}


def parse_transsion_version(raw_title: str, brand: str = '') -> dict[str, Any]:
    """Parse Transsion version strings into structured components.
    
    Handles both Classic Project Code naming and Modern OS-versioned naming.
    """
    clean = re.sub(r'^Tcard_', '', raw_title.strip())
    brand_norm = brand.lower()
    os_name = BRAND_TO_OS.get(brand_norm, 'Transsion OS')

    # Modern OS-versioned pattern: e.g. X6852-16.3.0.130SP08-EU002PF001AZ_a15, S686LN-15.1.2.118SP01-OP001PF001AZ
    m1 = re.match(r'^([A-Za-z0-9]+)-(\d+(?:\.\d+)+)(?:SP(\d+))?-([A-Z]{2})([A-Za-z0-9]+)?(?:_a(\d+))?$', clean)
    if m1:
        model, os_full, sp, reg, carrier, a_ver = m1.groups()
        sp_patch = f"SP{sp}" if sp else None
        return {
            'naming_style': 'modern_os',
            'model_code': model,
            'clean_version': clean,
            'display_version': clean,
            'region_code': reg or 'GL',
            'carrier_code': carrier,
            'os_name': os_name,
            'os_version': f"{os_full} {sp_patch}" if sp_patch else os_full,
            'patch_level': sp_patch,
            'android_version': a_ver or 'unknown',
            'build_date': None,
        }

    # Classic project pattern: e.g. CK6n-H6929CD-U-OP-240713V809, KB8-H627BNPQ-Q-200522V211
    m2 = re.match(r'^([A-Za-z0-9]+)-([A-Za-z0-9]+)-([A-Z])-(?:([A-Z]{2})-)?(\d{6})?(V\d+)?$', clean)
    if m2:
        model, board, a_letter, reg, yymmdd, build = m2.groups()
        android_v = LETTER_TO_ANDROID.get(a_letter, 'unknown')
        date_str = f"20{yymmdd[:2]}-{yymmdd[2:4]}-{yymmdd[4:6]}" if yymmdd and len(yymmdd) == 6 else None
        return {
            'naming_style': 'classic_project',
            'model_code': model,
            'board_code': board,
            'clean_version': clean,
            'display_version': clean,
            'region_code': reg or 'GL',
            'carrier_code': None,
            'os_name': os_name,
            'os_version': None,
            'build_number': build,
            'android_version': android_v,
            'build_date': date_str,
        }

    return {
        'naming_style': 'raw',
        'model_code': clean.split('-')[0] if '-' in clean else clean,
        'clean_version': clean,
        'display_version': clean,
        'region_code': 'GL',
        'carrier_code': None,
        'os_name': os_name,
        'os_version': None,
        'android_version': 'unknown',
        'build_date': None,
    }


def normalize_transsion_firmware(pkg: dict[str, Any] | TranssionFirmware) -> dict[str, Any]:
    """Normalize a raw Transsion firmware record to align with FindMyRom standard schema."""
    if isinstance(pkg, TranssionFirmware):
        d = asdict(pkg)
    else:
        d = dict(pkg)

    brand = d.get('brand', '').lower()
    title = d.get('title', '') or d.get('version', '')
    parsed = parse_transsion_version(title, brand=brand)

    # Secondary extraction of OS version from update changelog/description if missing
    desc = d.get('description', '') or ''
    os_ver = parsed.get('os_version')
    if not os_ver and desc:
        m_to = re.search(r'(?:to|upgrade to)\s+(?:HiOS|XOS|itel\s*OS|itelOS)\s*([0-9]+(?:\.[0-9]+)*)', desc, re.I)
        if m_to:
            os_ver = m_to.group(1)
        else:
            all_os = re.findall(r'(?:HiOS|XOS|itel\s*OS|itelOS)\s*([0-9]+(?:\.[0-9]+)*)', desc, re.I)
            if all_os:
                os_ver = all_os[-1]

    # Determine Android version (prefer explicit parsed version, fall back to SDK level)
    android_ver = parsed.get('android_version')
    if (not android_ver or android_ver == 'unknown') and d.get('sdk_level'):
        sdk = d['sdk_level']
        if sdk >= 36:
            android_ver = str(sdk - 20)
        elif sdk == 35:
            android_ver = '15'
        elif sdk == 34:
            android_ver = '14'
        elif sdk == 33:
            android_ver = '13'
        elif sdk in (31, 32):
            android_ver = '12'
        elif sdk == 30:
            android_ver = '11'
        elif sdk == 29:
            android_ver = '10'

    # Determine publication date
    pub_date = parsed.get('build_date')
    if not pub_date and d.get('build_timestamp'):
        import datetime
        pub_date = datetime.datetime.fromtimestamp(d['build_timestamp'], tz=datetime.timezone.utc).strftime('%Y-%m-%d')

    # Hash from download_url (Google GOTA CDN incorporates 40-hex SHA1 in URL)
    url = d.get('download_url', '') or ''
    sha1 = None
    m_hash = re.search(r'/package/([0-9a-fA-F]{40})\.zip', url)
    if m_hash:
        sha1 = m_hash.group(1).lower()

    size_bytes = int(d.get('size_bytes') or 0)
    size_str = f"{size_bytes / (1024**3):.2f} GB" if size_bytes else "Size unavailable"

    dev_id = (d.get('device_id') or '').strip()
    canonical_codename = dev_id.lower()
    if not canonical_codename.startswith(f"{brand}-"):
        canonical_codename = f"{brand}-{canonical_codename}"

    region_code = parsed.get('region_code') or 'GL'
    os_name = parsed.get('os_name') or BRAND_TO_OS.get(brand, 'Transsion OS')
    combined_system = f"Android {android_ver} · {os_name} {os_ver}" if os_ver else f"Android {android_ver} · {os_name}"

    return {
        'brand_id': brand,
        'brand_name': 'itel' if brand == 'itel' else brand.upper(),
        'device_codename': canonical_codename,
        'model_code': parsed.get('model_code') or dev_id,
        'marketing_name': d.get('marketing_name') or '',
        'display_version': parsed.get('display_version'),
        'raw_version': title,
        'region_code': region_code,
        'display_region': TRANSSION_REGIONS.get(region_code, region_code),
        'android_version': android_ver or 'unknown',
        'os_name': os_name,
        'os_version': os_ver,
        'combined_system': combined_system,
        'package_type': 'recovery_zip',
        'package_label': 'Recovery / OTA ZIP (Tcard 全量包)',
        'file_size_str': size_str,
        'file_size_bytes': size_bytes,
        'security_patch': d.get('security_patch'),
        'published_date': pub_date,
        'checksum_type': 'sha1',
        'checksum': sha1,
        'original_official_url': url,
        'fast_cdn_url': url,
        'board_code': parsed.get('board_code'),
        'build_number': parsed.get('build_number'),
        'precondition_fingerprint': d.get('precondition_fingerprint'),
        'postcondition_fingerprint': d.get('postcondition_fingerprint'),
        'payload_format': 'android_payload',
        'extractable_images': ['boot.img', 'init_boot.img', 'recovery.img', 'super.img'],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description='Transsion Official Device & Full Firmware Collector')
    parser.add_argument('--mode', choices=['devices', 'official-scan', 'probe', 'all'], default='devices',
                        help='Operation mode: extract device models, official-scan direct from Google checkin, probe fingerprint, or all')
    parser.add_argument('--brand', choices=['tecno', 'infinix', 'itel', 'all'], default='all',
                        help='Target brand')
    parser.add_argument('--output-dir', default='deliverables',
                        help='Output directory for generated artifacts')
    parser.add_argument('--fingerprint', help='Device fingerprint to checkin probe')
    parser.add_argument('--model', help='Device model for probe')
    parser.add_argument('--limit', type=int, default=20,
                        help='Limit number of firmware records to fetch')
    args = parser.parse_args()

    brands = ('tecno', 'infinix', 'itel') if args.brand == 'all' else (args.brand,)
    out_dir = Path(args.output_dir)

    if args.mode in ('devices', 'all'):
        print(f"[*] Extracting official Google Play certified models for: {', '.join(brands)}...")
        cats = fetch_google_play_devices(brands=brands)
        for b, devs in cats.items():
            print(f"    -> {b.upper()}: {len(devs)} models discovered")
        saved = export_device_catalogues(cats, out_dir)
        print("[+] Device catalogues written to:")
        for k, p in saved.items():
            print(f"    - {k}: {p}")

    if args.mode == 'probe':
        if not args.fingerprint:
            parser.error("--fingerprint is required for probe mode")
        print(f"[*] Probing Google GOTA checkin server for fingerprint:\n    {args.fingerprint}")
        fw = GoogleCheckinClient.probe(args.fingerprint, model=args.model)
        if fw:
            pkg_type = "全量包 (Full Package)" if not fw.is_incremental else "增量包 (Incremental OTA)"
            print("\n[+] Official firmware located!")
            print(f"    Type:         {pkg_type}")
            print(f"    Title:        {fw.title}")
            print(f"    Download URL: {fw.download_url}")
            print(f"    Size:         {fw.size_bytes:,} bytes ({fw.size_bytes / (1024*1024):.2f} MB)")
            print(f"    Target SDK:   Android SDK {fw.sdk_level}")
        else:
            print("[-] No updates returned by Google checkin for this fingerprint.")

    if args.mode in ('official-scan', 'all'):
        devices_file = out_dir / "transsion_devices.json"
        if not devices_file.exists():
            cats = fetch_google_play_devices(brands=brands)
            export_device_catalogues(cats, out_dir)

        with open(devices_file, 'r', encoding='utf-8') as f:
            all_devices = json.load(f)

        print(f"[*] Initiating DIRECT OFFICIAL SERVER batch scan across {len(all_devices)} certified models...")
        print(f"    Target server: https://android.googleapis.com/checkin")
        print(f"    Filter: FULL ROM ONLY (is_incremental=False, size > 500MB)")
        print(f"    Brand filter: {args.brand.upper()}")

        direct_fws = batch_probe_official_firmwares(
            all_devices,
            limit=args.limit,
            brand_filter=args.brand,
        )

        fw_out = out_dir / "transsion_full_firmwares.json"
        with open(fw_out, 'w', encoding='utf-8') as f:
            json.dump([asdict(fw) for fw in direct_fws], f, indent=2, ensure_ascii=False)

        summary_txt = out_dir / "transsion_full_firmwares.txt"
        with open(summary_txt, 'w', encoding='utf-8') as f:
            f.write("Brand\tDevice\tMarketing Name\tSize (GB)\tTitle\tOfficial Download URL\n")
            for fw in direct_fws:
                f.write(f"{fw.brand}\t{fw.device_id}\t{fw.marketing_name or ''}\t{fw.size_bytes/(1024**3):.2f}\t{fw.title}\t{fw.download_url}\n")

        print(f"\n[+] Successfully retrieved {len(direct_fws)} FULL ROM links directly from official server!")
        print(f"    - JSON metadata: {fw_out}")
        print(f"    - TSV summary:   {summary_txt}")


if __name__ == '__main__':
    main()
