"""Official Xiaomi / Redmi / POCO Stock Firmware Adapter (Google Play CSV + miotaV3.php + Official OSS CDN)."""

from __future__ import annotations

import base64
from collections.abc import Mapping, Sequence
import json
from pathlib import Path
import re
from typing import Any

from cryptography.hazmat.primitives import padding as sym_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
import httpx
import yaml

from fmr_core.publish_spec import canonical_version_str
from fmr_ingest_stock.cdn_rewriter import FAST_HOST, validate_official_download_url
from fmr_ingest_stock.pipeline.verifier import format_file_size

from .base import (
    AdapterDevice,
    AdapterRelease,
    OfficialMetadataHTTP,
    VendorAdapter,
)
from .google_play_devices import (
    GOOGLE_PLAY_DEVICES_URL,
    fetch_google_play_devices,
)

MIOTA_V3_URL = "https://update.miui.com/updates/miotaV3.php"
MIOTA_AES_KEY = b"miuiotavalided11"
MIOTA_AES_IV = b"0102030405060708"
ULTIMATE_OTA_HOST = "ultimateota.d.miui.com"
FAST_OSS_HOST = FAST_HOST

DEFAULT_DEVICE_TAGS_PATH = (
    Path(__file__).resolve().parents[2] / "config" / "xiaomi_device_tags.yaml"
)

XIAOMI_PLAY_BRANDS: tuple[str, ...] = ("Xiaomi", "Redmi", "POCO")

# Maps 4-letter MIUI/HyperOS region tag (*XM) -> canonical FMR region_code.
XM_SUFFIX_TO_REGION: dict[str, str] = {
    "CNXM": "CN",
    "MIXM": "Global",
    "EUXM": "EEA",
    "INXM": "IN",
    "IDXM": "ID",
    "RUXM": "RU",
    "TRXM": "TR",
    "TWXM": "TW",
    "JPXM": "JP",
    "SGXM": "SG",
}

# Maps canonical region -> (reg_xm, pn_suffix, zone, r_code, locale)
REGION_SPECS: dict[str, tuple[str, str, int, str, str]] = {
    "CN": ("CNXM", "", 1, "CN", "zh_CN"),
    "Global": ("MIXM", "_global", 2, "GL", "en_US"),
    "EEA": ("EUXM", "_eea_global", 2, "EEA", "en_US"),
    "IN": ("INXM", "_in_global", 2, "IN", "en_US"),
    "ID": ("IDXM", "_id_global", 2, "ID", "en_US"),
    "RU": ("RUXM", "_ru_global", 2, "RU", "en_US"),
    "TR": ("TRXM", "_tr_global", 2, "TR", "en_US"),
    "TW": ("TWXM", "_tw_global", 2, "TW", "en_US"),
    "JP": ("JPXM", "_jp_global", 2, "JP", "en_US"),
    "SG": ("SGXM", "_sg_global", 2, "SG", "en_US"),
}

_REGION_ALIASES: dict[str, str] = {
    "cn": "CN",
    "cnxm": "CN",
    "global": "Global",
    "mi": "Global",
    "gl": "Global",
    "mixm": "Global",
    "eea": "EEA",
    "eu": "EEA",
    "euxm": "EEA",
    "in": "IN",
    "inxm": "IN",
    "id": "ID",
    "idxm": "ID",
    "ru": "RU",
    "ruxm": "RU",
    "tr": "TR",
    "trxm": "TR",
    "tw": "TW",
    "twxm": "TW",
    "jp": "JP",
    "jpxm": "JP",
    "sg": "SG",
    "sgxm": "SG",
}

_CODENAME_REGION_SUFFIXES: tuple[tuple[str, str], ...] = (
    ("_eea_global", "EEA"),
    ("_in_global", "IN"),
    ("_id_global", "ID"),
    ("_ru_global", "RU"),
    ("_tr_global", "TR"),
    ("_tw_global", "TW"),
    ("_jp_global", "JP"),
    ("_sg_global", "SG"),
    ("_global", "Global"),
    ("_eea", "EEA"),
    ("_in", "IN"),
    ("_cn", "CN"),
)

_RE_TAG = re.compile(r"^[A-Z]{2}$")
_RE_CODENAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_RE_MD5_HEX = re.compile(r"^[0-9a-f]{32}$")
_RE_XM_REGION = re.compile(r"([A-Z]{2}XM)(?:$|\.)")
_RE_SAFE_ZIP_NAME = re.compile(r"^[A-Za-z0-9._+-]+\.zip$", re.IGNORECASE)
_RE_EXCLUDED_ACCESSORY = re.compile(
    r"(?:\b(?:watch|band|buds|earbuds|router|scooter|tv|stick|camera|speaker)\b|mitv|mibox|miproj|telebee)",
    re.IGNORECASE,
)


def normalize_region_name(raw_region: str) -> str | None:
    key = str(raw_region or "").strip().lower()
    return _REGION_ALIASES.get(key)


def normalize_xiaomi_base_codename(raw_codename: str) -> tuple[str, str | None]:
    """Strip regional Xiaomi codename suffixes (`_global`, `_eea_global`, etc.) into `(base_code, region)`."""
    cleaned = str(raw_codename or "").strip().lower()
    for suffix, region in _CODENAME_REGION_SUFFIXES:
        if cleaned.endswith(suffix) and len(cleaned) > len(suffix):
            base = cleaned[: -len(suffix)]
            if _RE_CODENAME.fullmatch(base):
                return base, region
    return cleaned, None


def encrypt_miota_payload(payload: Mapping[str, Any] | str | bytes) -> str:
    """Encrypt a miotaV3 JSON payload using AES-128-CBC + PKCS7 + URL-safe Base64."""
    if isinstance(payload, bytes):
        raw_bytes = payload
    elif isinstance(payload, str):
        raw_bytes = payload.encode("utf-8")
    else:
        raw_bytes = json.dumps(dict(payload), separators=(",", ":"), sort_keys=False).encode(
            "utf-8"
        )

    padder = sym_padding.PKCS7(128).padder()
    padded = padder.update(raw_bytes) + padder.finalize()

    cipher = Cipher(algorithms.AES(MIOTA_AES_KEY), modes.CBC(MIOTA_AES_IV))
    encryptor = cipher.encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()
    return base64.urlsafe_b64encode(ciphertext).decode("ascii")


def decrypt_miota_payload(encoded: str | bytes) -> dict[str, Any]:
    """Decrypt a URL-safe Base64 AES-128-CBC response from official miotaV3.php."""
    text = encoded.decode("utf-8") if isinstance(encoded, bytes) else str(encoded)
    compact = re.sub(r"\s+", "", text).replace("+", "-").replace("/", "_")
    if not compact:
        raise ValueError("Empty miotaV3 payload")
    pad_len = (-len(compact)) % 4
    if pad_len:
        compact += "=" * pad_len

    ciphertext = base64.urlsafe_b64decode(compact.encode("ascii"))
    if not ciphertext or len(ciphertext) % 16 != 0:
        raise ValueError("Invalid AES-128-CBC ciphertext length in miotaV3 payload")

    cipher = Cipher(algorithms.AES(MIOTA_AES_KEY), modes.CBC(MIOTA_AES_IV))
    decryptor = cipher.decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()

    unpadder = sym_padding.PKCS7(128).unpadder()
    plaintext = unpadder.update(padded) + unpadder.finalize()
    parsed = json.loads(plaintext.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError("Decrypted miotaV3 payload is not a JSON object")
    return parsed


_ANDROID_SEED_PREFIXES: dict[int, str] = {
    8: "V10.0.1.0.O",
    9: "V11.0.1.0.P",
    10: "V12.0.1.0.Q",
    11: "V12.5.1.0.R",
    12: "V13.0.1.0.S",
    13: "V14.0.1.0.T",
    14: "V816.0.1.0.U",
    15: "OS2.0.1.0.V",
    16: "OS3.0.1.0.W",
    17: "OS4.0.1.0.X",
}


def build_miota_seed_version(android_major: int, tag: str, reg_xm: str) -> str:
    """Build seed MIUI/HyperOS version (`cv`) for a given Android major, 2-letter tag, and *XM region."""
    norm_tag = tag.strip().upper()
    norm_reg = reg_xm.strip().upper()
    if not _RE_TAG.fullmatch(norm_tag):
        raise ValueError(f"Invalid 2-letter Xiaomi device tag: {tag!r}")

    prefix = _ANDROID_SEED_PREFIXES.get(android_major)
    if prefix is None:
        raise ValueError(f"Unsupported Xiaomi seed Android major version: {android_major}")
    return f"{prefix}{norm_tag}{norm_reg}"


def build_miota_request_payload(
    *,
    base_code: str,
    region: str,
    android_major: int,
    tag: str,
) -> dict[str, Any]:
    """Build the live-verified official miotaV3 full updater payload."""
    canon_region = normalize_region_name(region)
    if canon_region is None or canon_region not in REGION_SPECS:
        raise ValueError(f"Unsupported Xiaomi region: {region!r}")
    reg_xm, pn_suffix, zone, r_code, locale = REGION_SPECS[canon_region]
    regional_codename = f"{base_code}{pn_suffix}"
    cv = build_miota_seed_version(android_major, tag, reg_xm)
    return {
        "c": str(android_major),
        "d": regional_codename,
        "b": "F",
        "f": "1",
        "ov": f"MIUI-{cv}",
        "l": locale,
        "r": r_code,
        "v": f"MIUI-{cv}",
        "p": base_code,
        "pn": regional_codename,
        "options": {
            "zone": zone,
            "sv": 3,
            "av": "8.8.0",
            "cv": cv,
        },
    }


def load_xiaomi_device_tags(
    yaml_path: str | Path | None = None,
) -> dict[str, dict[str, Any]]:
    """Load `config/xiaomi_device_tags.yaml` via `yaml.safe_load`."""
    path = Path(yaml_path) if yaml_path is not None else DEFAULT_DEVICE_TAGS_PATH
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"Expected mapping in {path}")
    source_map = raw.get("devices") if isinstance(raw.get("devices"), dict) else raw

    normalized: dict[str, dict[str, Any]] = {}
    for raw_code, entry in source_map.items():
        if not isinstance(entry, dict):
            continue
        code = str(raw_code).strip().lower()
        if not _RE_CODENAME.fullmatch(code):
            continue
        raw_tag_val = entry.get("tag")
        if raw_tag_val is False:
            tag = "NO"
        elif raw_tag_val is True:
            tag = "ON"
        else:
            tag = str(raw_tag_val or "").strip().upper()
        if not _RE_TAG.fullmatch(tag):
            raise ValueError(f"Invalid 2-letter tag {tag!r} for Xiaomi device {code!r}")
        marketing_name = str(entry.get("marketing_name") or code).strip()
        raw_models = entry.get("models") or ()
        models = [str(m).strip() for m in raw_models if str(m).strip()]

        raw_android = entry.get("android_versions") or ()
        android_versions: list[int] = []
        for av in raw_android:
            av_int = int(str(av).strip().split(".")[0])
            if av_int not in android_versions:
                android_versions.append(av_int)

        raw_regions = entry.get("regions") or ()
        regions: list[str] = []
        for r in raw_regions:
            norm_r = normalize_region_name(str(r))
            if norm_r and norm_r not in regions:
                regions.append(norm_r)

        raw_aliases = entry.get("aliases") or ()
        aliases: list[str] = []
        for al in raw_aliases:
            al_code = str(al).strip().lower()
            if al_code and al_code != code and _RE_CODENAME.fullmatch(al_code) and al_code not in aliases:
                aliases.append(al_code)

        normalized[code] = {
            "tag": tag,
            "marketing_name": marketing_name,
            "models": models,
            "android_versions": android_versions,
            "regions": regions,
            "aliases": aliases,
        }
    return normalized


def _format_xiaomi_marketing_name(retail_branding: str, raw_name: str) -> str:
    clean_name = re.sub(r"\s+", " ", raw_name.strip())
    clean_brand = re.sub(r"\s+", " ", retail_branding.strip())
    if not clean_name:
        return clean_brand or "Xiaomi Device"
    lower_name = clean_name.lower()
    if lower_name.startswith(("xiaomi ", "redmi ", "poco ", "mi ", "pocophone ")):
        return clean_name
    if clean_brand and not lower_name.startswith(clean_brand.lower()):
        return f"{clean_brand} {clean_name}"
    return clean_name


def _normalize_android_version_str(raw_codebase: Any, fallback_major: int) -> str:
    cleaned = str(raw_codebase or "").strip()
    if not cleaned:
        return str(fallback_major)
    if re.fullmatch(r"\d+\.0+", cleaned):
        return cleaned.split(".", 1)[0]
    return cleaned


def _extract_region_from_version(version_str: str, fallback_region: str) -> str:
    upper = version_str.strip().upper()
    m = _RE_XM_REGION.search(upper)
    if m:
        mapped = XM_SUFFIX_TO_REGION.get(m.group(1))
        if mapped:
            return mapped
    for xm_code, region_label in XM_SUFFIX_TO_REGION.items():
        if upper.endswith(xm_code):
            return region_label
    return fallback_region


def _extract_os_info(version_str: str) -> tuple[str, str | None]:
    cleaned = version_str.strip()
    upper = cleaned.upper()
    if upper.startswith("OS"):
        m = re.match(r"^OS(\d+(?:\.\d+){1,3})", upper)
        return "HyperOS", (m.group(1) if m else None)
    m_miui = re.match(r"^V(\d+(?:\.\d+){1,3})", upper)
    return "MIUI", (m_miui.group(1) if m_miui else None)


class XiaomiAdapter(VendorAdapter):
    """100% official Xiaomi / Redmi / POCO stock firmware adapter."""

    vendor_id = "xiaomi"

    def __init__(
        self,
        *,
        device_tags_path: str | Path | None = None,
        play_csv_url: str = GOOGLE_PLAY_DEVICES_URL,
        miota_url: str = MIOTA_V3_URL,
    ) -> None:
        self.device_tags_path = (
            Path(device_tags_path) if device_tags_path is not None else DEFAULT_DEVICE_TAGS_PATH
        )
        self.device_tags = load_xiaomi_device_tags(self.device_tags_path)
        self.play_csv_url = play_csv_url
        self.miota_url = miota_url

    def discover_devices(self, http: OfficialMetadataHTTP) -> list[AdapterDevice]:
        records = fetch_google_play_devices(
            http,
            brands=XIAOMI_PLAY_BRANDS,
            include_wearables=False,
            url=self.play_csv_url,
        )

        alias_to_base: dict[str, str] = {}
        for code, entry in self.device_tags.items():
            if not isinstance(entry, dict):
                continue
            for raw_alias in entry.get("aliases") or ():
                alias_code = str(raw_alias).strip().lower()
                if (
                    alias_code
                    and alias_code != code
                    and alias_code not in self.device_tags
                    and _RE_CODENAME.fullmatch(alias_code)
                ):
                    alias_to_base.setdefault(alias_code, code)

        play_models: dict[str, list[str]] = {}
        play_names: dict[str, list[str]] = {}
        play_raw_codenames: dict[str, list[str]] = {}
        play_regions: dict[str, list[str]] = {}

        for rec in records:
            if _RE_EXCLUDED_ACCESSORY.search(f"{rec.marketing_name} {rec.model}"):
                continue
            base_code, inferred_region = normalize_xiaomi_base_codename(rec.codename)
            if not base_code or base_code == "__global__" or not _RE_CODENAME.fullmatch(base_code):
                continue
            base_code = alias_to_base.get(base_code, base_code)

            play_models.setdefault(base_code, [])
            play_names.setdefault(base_code, [])
            play_raw_codenames.setdefault(base_code, [])
            play_regions.setdefault(base_code, [])

            if rec.model and rec.model not in play_models[base_code]:
                play_models[base_code].append(rec.model)

            formatted_name = _format_xiaomi_marketing_name(rec.retail_branding, rec.marketing_name)
            if formatted_name and formatted_name not in play_names[base_code]:
                play_names[base_code].append(formatted_name)

            if rec.codename not in play_raw_codenames[base_code]:
                play_raw_codenames[base_code].append(rec.codename)

            if inferred_region and inferred_region not in play_regions[base_code]:
                play_regions[base_code].append(inferred_region)

        tagged_codes = sorted(
            code
            for code, entry in self.device_tags.items()
            if isinstance(entry, dict)
            and _RE_TAG.fullmatch(str(entry.get("tag") or "").strip().upper())
        )
        if not tagged_codes:
            return []

        devices: list[AdapterDevice] = []

        for code in tagged_codes:
            tag_entry = self.device_tags[code]
            merged_models: list[str] = []
            for m in tag_entry["models"]:
                if m not in merged_models:
                    merged_models.append(m)
            for m in play_models.get(code, ()):
                if m not in merged_models:
                    merged_models.append(m)

            yaml_name = str(tag_entry.get("marketing_name") or "").strip()
            candidates = play_names.get(code, ())
            if yaml_name and yaml_name.lower() != code.lower():
                marketing_name = yaml_name
            elif candidates:
                marketing_name = candidates[0]
            else:
                marketing_name = yaml_name or f"Xiaomi {code}"

            merged_regions: list[str] = []
            for r in tag_entry["regions"]:
                if r not in merged_regions:
                    merged_regions.append(r)
            for r in play_regions.get(code, ()):
                if r not in merged_regions:
                    merged_regions.append(r)

            android_versions = list(tag_entry["android_versions"])
            boot_partition = (
                "boot"
                if android_versions and max(android_versions) < 13
                else "init_boot"
            )

            vendor_meta: dict[str, Any] = {
                "models": merged_models,
                "regions": merged_regions,
                "raw_codenames": list(play_raw_codenames.get(code, (code,))),
                "tag": str(tag_entry["tag"]).strip().upper(),
                "android_versions": android_versions,
            }

            devices.append(
                AdapterDevice(
                    codename=code,
                    brand_id="xiaomi",
                    marketing_name=marketing_name,
                    model_numbers=tuple(merged_models),
                    boot_partition_type=boot_partition,
                    has_vbmeta=1,
                    vendor_meta=vendor_meta,
                )
            )

        return devices

    def fetch_releases(
        self,
        device: AdapterDevice,
        http: OfficialMetadataHTTP,
    ) -> list[AdapterRelease]:
        base_code, suffix_region = normalize_xiaomi_base_codename(device.codename)
        tag_entry = self.device_tags.get(base_code)
        meta = device.vendor_meta if isinstance(device.vendor_meta, dict) else {}

        raw_tag = str(meta.get("tag") or (tag_entry["tag"] if tag_entry else "")).strip().upper()
        if not _RE_TAG.fullmatch(raw_tag):
            return []

        raw_avs = meta.get("android_versions") or (
            tag_entry["android_versions"] if tag_entry else ()
        )
        android_versions: list[int] = []
        for av in raw_avs:
            try:
                av_int = int(str(av).strip().split(".")[0])
            except ValueError:
                continue
            if 8 <= av_int <= 17 and av_int not in android_versions:
                android_versions.append(av_int)
        if not android_versions:
            android_versions = [14, 15, 16]

        raw_regions = meta.get("regions")
        regions: list[str] = []
        if isinstance(raw_regions, Sequence) and not isinstance(raw_regions, (str, bytes)):
            for r in raw_regions:
                norm_r = normalize_region_name(str(r))
                if norm_r and norm_r not in regions:
                    regions.append(norm_r)
        if not regions:
            if suffix_region:
                regions = [suffix_region]
            elif tag_entry and tag_entry["regions"]:
                regions = list(tag_entry["regions"])
            else:
                regions = ["CN", "Global", "EEA"]

        releases: list[AdapterRelease] = []
        seen_keys: set[tuple[str, str, str]] = set()

        for region in regions:
            if region not in REGION_SPECS:
                continue
            _, pn_suffix, _, _, _ = REGION_SPECS[region]
            regional_codename = f"{base_code}{pn_suffix}"

            for android_major in android_versions:
                req_payload = build_miota_request_payload(
                    base_code=base_code,
                    region=region,
                    android_major=android_major,
                    tag=raw_tag,
                )
                enc_q = encrypt_miota_payload(req_payload)
                form_body = f"q={enc_q}&t=&s=1"

                try:
                    resp = http.post(
                        self.miota_url,
                        content=form_body,
                        headers={"Content-Type": "application/x-www-form-urlencoded"},
                    )
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code in (400, 403, 404, 410):
                        continue
                    raise

                if not resp.content or not resp.content.strip():
                    continue

                try:
                    decoded = decrypt_miota_payload(resp.content)
                except (ValueError, json.JSONDecodeError):
                    continue

                for rom_slot in ("LatestRom", "CurrentRom"):
                    rom_obj = decoded.get(rom_slot)
                    if not isinstance(rom_obj, dict) or not rom_obj:
                        continue

                    parsed_rel = self._parse_rom_object(
                        device=device,
                        regional_codename=regional_codename,
                        queried_region=region,
                        queried_android_major=android_major,
                        rom_slot=rom_slot,
                        rom_obj=rom_obj,
                    )
                    if parsed_rel is None:
                        continue

                    dedup_key = (
                        parsed_rel.region_code,
                        parsed_rel.canonical_version,
                        parsed_rel.original_filename or "",
                    )
                    if dedup_key in seen_keys:
                        continue
                    seen_keys.add(dedup_key)
                    releases.append(parsed_rel)

        return releases

    def _parse_rom_object(
        self,
        *,
        device: AdapterDevice,
        regional_codename: str,
        queried_region: str,
        queried_android_major: int,
        rom_slot: str,
        rom_obj: Mapping[str, Any],
    ) -> AdapterRelease | None:
        raw_ver = str(rom_obj.get("version") or "").strip()
        if raw_ver.upper().startswith("MIUI-"):
            raw_ver = raw_ver[5:].strip()
        if not raw_ver or "/" in raw_ver or ".." in raw_ver:
            return None

        raw_filename = str(rom_obj.get("filename") or "").strip()
        if not raw_filename:
            return None
        clean_filename = raw_filename.split("?", 1)[0].split("#", 1)[0].strip().lstrip("/")
        if "/" in clean_filename or ".." in clean_filename:
            return None
        if not _RE_SAFE_ZIP_NAME.fullmatch(clean_filename):
            return None

        lower_fname = clean_filename.lower()
        rom_type_field = str(rom_obj.get("type") or "").strip().lower()
        if (
            "blockota" in lower_fname
            or "incremental" in lower_fname
            or rom_type_field in ("blockota", "incremental")
        ):
            return None

        raw_md5 = str(rom_obj.get("md5") or "").strip().lower()
        if not _RE_MD5_HEX.fullmatch(raw_md5):
            return None

        fast_url = f"https://{FAST_OSS_HOST}/{raw_ver}/{clean_filename}"
        orig_url = f"https://{ULTIMATE_OTA_HOST}/{raw_ver}/{clean_filename}"
        try:
            validate_official_download_url("xiaomi", fast_url, resolve_dns=False)
            validate_official_download_url("xiaomi", orig_url, resolve_dns=False)
        except ValueError:
            return None

        region_code = _extract_region_from_version(raw_ver, queried_region)
        android_version = _normalize_android_version_str(
            rom_obj.get("codebase"), queried_android_major
        )
        os_name, os_version = _extract_os_info(raw_ver)

        size_bytes: int | None = None
        size_str: str | None = None
        raw_filesize = rom_obj.get("filesize")
        if isinstance(raw_filesize, int) and not isinstance(raw_filesize, bool) and raw_filesize > 0:
            size_bytes = raw_filesize
            size_str = format_file_size(size_bytes)
        elif isinstance(raw_filesize, str) and raw_filesize.strip().isdigit():
            parsed_int = int(raw_filesize.strip())
            if parsed_int > 0:
                size_bytes = parsed_int
                size_str = format_file_size(size_bytes)

        return AdapterRelease(
            device_codename=device.codename,
            model_code=regional_codename,
            region_code=region_code,
            version_str=canonical_version_str(raw_ver),
            android_version=android_version,
            package_type="recovery_zip",
            original_official_url=orig_url,
            fast_cdn_url=fast_url,
            md5_or_sha256=raw_md5,
            file_size_bytes=size_bytes,
            file_size_str=size_str,
            delivery_mode="direct_cdn",
            verification_status="VERIFIED_IMMUTABLE",
            display_version=raw_ver,
            raw_version=raw_ver,
            os_name=os_name,
            os_version=os_version,
            original_filename=clean_filename,
            vendor_fields={
                "regional_codename": regional_codename,
                "rom_slot": rom_slot,
                "branch": str(rom_obj.get("branch") or "F"),
            },
            sources={"miota_v3": self.miota_url},
            availability="available",
        )


__all__ = [
    "DEFAULT_DEVICE_TAGS_PATH",
    "FAST_OSS_HOST",
    "MIOTA_AES_IV",
    "MIOTA_AES_KEY",
    "MIOTA_V3_URL",
    "REGION_SPECS",
    "ULTIMATE_OTA_HOST",
    "XM_SUFFIX_TO_REGION",
    "XiaomiAdapter",
    "build_miota_request_payload",
    "build_miota_seed_version",
    "decrypt_miota_payload",
    "encrypt_miota_payload",
    "load_xiaomi_device_tags",
    "normalize_region_name",
    "normalize_xiaomi_base_codename",
]
