"""Official OPlus (OnePlus / OPPO / realme) Stock Firmware Adapter.

Uses Google Play `supported_devices.csv` for official phone/tablet discovery and
live-verified `/update/v3` PC-channel encrypted queries (`zero_tail_seed_version`)
for Full-OTA recovery packages (`type == "1"`, `gkaReq == 0`, `>= 500 MiB`).
"""

from __future__ import annotations

from collections.abc import Sequence
import datetime
from pathlib import PurePosixPath
import re
from typing import Any
from urllib.parse import unquote, urlsplit

import httpx

from fmr_core.publish_spec import canonical_version_str
from fmr_ingest_stock import download_refresh
from fmr_ingest_stock.cdn_rewriter import validate_official_download_url
from fmr_ingest_stock.device_identity import marketing_name as format_marketing_name
from fmr_ingest_stock.oem_sources import canonical_code
from fmr_ingest_stock.oplus_ota import (
    PC_PROBE_BRANCHES,
    REGION_CONFIGS,
    OPlusGateError,
    OPlusOTAClient,
    clean_oplus_cdn_url,
    zero_tail_seed_version,
)
from fmr_ingest_stock.release_metadata import iso_date

from .base import AdapterDevice, AdapterRelease, OfficialMetadataHTTP, VendorAdapter
from .google_play_devices import GOOGLE_PLAY_DEVICES_URL, fetch_google_play_devices

SUPPORTED_OPLUS_VENDORS: tuple[str, ...] = ("oneplus", "oppo", "realme")

VENDOR_OS_NAMES: dict[str, str] = {
    "oneplus": "OxygenOS",
    "oppo": "ColorOS",
    "realme": "realme UI",
}

OPLUS_OTA_MODEL_RE = re.compile(
    r"^(?:"
    r"CPH\d{4}"
    r"|RMX\d{4}"
    r"|RMP\d{4}"
    r"|P[A-Z]{2}(?:\d{3}|[A-Z0-9]\d{2})"
    r"|AC\d{4}"
    r"|BE\d{4}"
    r"|DE\d{4}"
    r"|DN\d{4}"
    r"|EB\d{4}"
    r"|IV\d{4}"
    r"|GN\d{4}"
    r"|NE\d{4}"
    r"|LE\d{4}"
    r"|MT\d{4}"
    r"|KB\d{4}"
    r"|IN\d{4}"
    r"|GM\d{4}"
    r"|HD\d{4}"
    r"|OPD\d{4}"
    r")$"
)

_RE_DOMESTIC_CN_MODEL = re.compile(r"^P[A-Z]{2}(?:\d{3}|[A-Z0-9]\d{2})$")
_RE_CPH_SUFFIX = re.compile(r"^(CPH\d{4})(?:FW|RU)$")
_RE_OPD_SUFFIX = re.compile(r"^(OPD\d{4})[A-Z]$")
_RE_ONEPLUS_NORD_GLOBAL_MODEL = re.compile(r"^(?:AC|BE|DE|DN|EB|IV|GN)\d{4}$")
_RE_ONEPLUS_NUMERIC_MODEL = re.compile(r"^(?:NE|LE|MT|KB|IN|GM|HD|OPD)\d{4}$")
_RE_CHECKSUM = re.compile(r"^(?:[0-9a-f]{32}|[0-9a-f]{64})$")
_RE_ANDROID_PREFIX = re.compile(r"^android\s+", re.IGNORECASE)
_RE_OS_VER_NUM = re.compile(r"(\d+(?:\.\d+)*)")
_RE_OTA_TS = re.compile(r"_(20\d{2})(\d{2})(\d{2})\d{4}$")
_RE_SAFE_REFRESH_FILENAME = re.compile(r"^[A-Za-z0-9_.()+-]{1,240}\.zip$", re.IGNORECASE)


def format_bytes(size_bytes: int) -> str:
    """Format byte count into human-readable unit string."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    units = ("KB", "MB", "GB", "TB")
    val = float(size_bytes)
    for unit in units:
        val /= 1024.0
        if val < 1024.0 or unit == units[-1]:
            return f"{val:.2f} {unit}"
    return f"{size_bytes} B"


def normalize_oplus_model(raw_model: str) -> str | None:
    """Normalize and validate an OPlus OTA-capable smartphone/tablet model number."""
    cleaned = (raw_model or "").strip().upper()
    m_cph = _RE_CPH_SUFFIX.fullmatch(cleaned)
    if m_cph:
        cleaned = m_cph.group(1)
    else:
        m_opd = _RE_OPD_SUFFIX.fullmatch(cleaned)
        if m_opd:
            cleaned = m_opd.group(1)
    if not OPLUS_OTA_MODEL_RE.fullmatch(cleaned):
        return None
    return cleaned


def select_regions_for_model(model: str) -> tuple[str, ...]:
    """Select target OPlus PC-OTA regions per hardware model prefix."""
    clean = model.strip().upper()
    if clean.startswith("CPH"):
        return ("SG", "EU", "IN")
    if clean.startswith("RMP"):
        return ("SG", "IN", "EU")
    if clean.startswith("RMX"):
        return ("SG", "IN", "EU", "CN")
    if _RE_DOMESTIC_CN_MODEL.fullmatch(clean):
        return ("CN",)
    if _RE_ONEPLUS_NORD_GLOBAL_MODEL.fullmatch(clean):
        return ("SG", "EU", "IN")
    if _RE_ONEPLUS_NUMERIC_MODEL.fullmatch(clean):
        return ("CN", "SG", "EU", "IN")
    return ("SG", "EU", "IN", "CN")


def _normalize_security_patch(raw_patch: str | None) -> str | None:
    if not raw_patch:
        return None
    cleaned = raw_patch.strip()
    if not cleaned:
        return None
    if re.fullmatch(r"20\d{6}", cleaned):
        cleaned = f"{cleaned[:4]}-{cleaned[4:6]}-{cleaned[6:8]}"
    parsed = iso_date(cleaned)
    if parsed and re.fullmatch(r"\d{4}-\d{2}-\d{2}", parsed):
        return parsed
    parts = cleaned.replace("/", "-").replace(".", "-").split("-")
    if len(parts) == 3 and all(p.isdigit() for p in parts):
        try:
            dt = datetime.date(int(parts[0]), int(parts[1]), int(parts[2]))
            if 2014 <= dt.year <= 2099:
                return dt.isoformat()
        except ValueError:
            return None
    return None


def _extract_ota_published_date(ota_target_version: str | None) -> str | None:
    if not ota_target_version:
        return None
    m = _RE_OTA_TS.search(ota_target_version.strip())
    if not m:
        return None
    try:
        dt = datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if 2015 <= dt.year <= 2099:
            return dt.isoformat()
    except ValueError:
        return None
    return None


def _normalize_android_version(raw_android: str | None) -> str:
    if not raw_android:
        return "unknown"
    cleaned = _RE_ANDROID_PREFIX.sub("", raw_android.strip()).strip()
    return cleaned or "unknown"


def _normalize_os_version(raw_os: str | None) -> str | None:
    if not raw_os:
        return None
    m = _RE_OS_VER_NUM.search(raw_os.strip())
    return m.group(1) if m else None


def _normalize_checksum(raw_hash: str | None) -> str | None:
    if not raw_hash:
        return None
    cleaned = raw_hash.strip().lower()
    return cleaned if _RE_CHECKSUM.fullmatch(cleaned) else None


def _sanitize_zip_filename(raw_filename: str, fallback_stem: str) -> str | None:
    decoded = unquote(raw_filename or "").strip()
    if not decoded.lower().endswith(".zip"):
        return None
    if _RE_SAFE_REFRESH_FILENAME.fullmatch(decoded):
        return decoded
    stem = re.sub(r"[^A-Za-z0-9_.()+-]+", "_", decoded[:-4]).strip("_") or re.sub(
        r"[^A-Za-z0-9_.()+-]+", "_", fallback_stem
    )
    candidate = f"{stem[:230]}.zip"
    return candidate if _RE_SAFE_REFRESH_FILENAME.fullmatch(candidate) else None


def _is_oplus_gate_url(url: str) -> bool:
    parsed = urlsplit(url)
    host_lower = (parsed.hostname or "").lower()
    return (
        parsed.scheme == "https"
        and parsed.path == "/downloadCheck"
        and (
            host_lower.endswith(".allawnos.com")
            or host_lower.endswith(".allawntech.com")
        )
    )


def _build_oplus_refresh_descriptor(
    *,
    vendor_id: str,
    version_name: str,
    gate_url: str,
    filename: str,
) -> tuple[dict[str, Any], str]:
    refresh_source: dict[str, Any] = {
        "schema": 1,
        "vendor": vendor_id,
        "version": version_name,
        "gate_url": gate_url,
        "filename": filename,
    }
    download_refresh.validate(refresh_source)
    hasher = getattr(download_refresh, "source_hash", download_refresh.fingerprint)
    digest = hasher(refresh_source)
    refresh_url = f"{download_refresh.PREFIX}{digest}"
    return refresh_source, refresh_url


class OPlusAdapter(VendorAdapter):
    """Official v4 stock-firmware adapter for OnePlus, OPPO, and realme."""

    def __init__(
        self,
        vendor_id: str,
        *,
        branches: Sequence[str] = PC_PROBE_BRANCHES,
        play_csv_url: str = GOOGLE_PLAY_DEVICES_URL,
    ) -> None:
        clean_vendor = (vendor_id or "").strip().lower()
        if clean_vendor not in SUPPORTED_OPLUS_VENDORS:
            raise ValueError(
                f"OPlusAdapter vendor_id must be one of {SUPPORTED_OPLUS_VENDORS}, got {vendor_id!r}"
            )
        cleaned_branches = tuple(
            dict.fromkeys(
                b.strip().upper()
                for b in branches
                if b and re.fullmatch(r"[A-Za-z0-9]{1,4}", b.strip())
            )
        )
        if not cleaned_branches:
            raise ValueError("OPlusAdapter requires at least one valid OTA branch")
        self.vendor_id = clean_vendor
        self.branches = cleaned_branches
        self.play_csv_url = play_csv_url

    def discover_devices(self, http: OfficialMetadataHTTP) -> list[AdapterDevice]:
        records = fetch_google_play_devices(
            http,
            brands=self.vendor_id,
            include_wearables=False,
            url=self.play_csv_url,
        )

        models_by_code: dict[str, list[str]] = {}
        names_by_code: dict[str, list[str]] = {}
        raw_codenames_by_code: dict[str, list[str]] = {}

        for rec in records:
            ota_model = normalize_oplus_model(rec.model)
            if ota_model is None:
                continue
            code = canonical_code(self.vendor_id, rec.codename)
            if not code or code == "__global__":
                continue

            models_by_code.setdefault(code, [])
            names_by_code.setdefault(code, [])
            raw_codenames_by_code.setdefault(code, [])

            if ota_model not in models_by_code[code]:
                models_by_code[code].append(ota_model)
            if rec.marketing_name and rec.marketing_name not in names_by_code[code]:
                names_by_code[code].append(rec.marketing_name)
            if rec.codename not in raw_codenames_by_code[code]:
                raw_codenames_by_code[code].append(rec.codename)

        devices: list[AdapterDevice] = []
        brand_title = {"oneplus": "OnePlus", "oppo": "OPPO", "realme": "realme"}[self.vendor_id]

        for code in sorted(models_by_code):
            ota_models = models_by_code[code]
            if not ota_models:
                continue
            candidate_names = names_by_code.get(code, [])
            chosen_raw = candidate_names[0] if candidate_names else ota_models[0]
            for cand in candidate_names:
                if cand.strip().upper() not in ota_models:
                    chosen_raw = cand
                    break

            clean_label = format_marketing_name(
                {
                    "vendor": self.vendor_id,
                    "upstream_id": code,
                    "codename": code,
                    "name": chosen_raw,
                    "models": ",".join(ota_models),
                }
            )
            devices.append(
                AdapterDevice(
                    codename=code,
                    brand_id=self.vendor_id,
                    marketing_name=clean_label or f"{brand_title} {ota_models[0]}",
                    model_numbers=tuple(ota_models),
                    boot_partition_type="init_boot",
                    has_vbmeta=1,
                    vendor_meta={
                        "models": list(ota_models),
                        "raw_codenames": list(raw_codenames_by_code.get(code, ())),
                    },
                )
            )
        return devices

    def _extract_device_models(self, device: AdapterDevice) -> list[str]:
        raw_candidates: list[str] = []
        meta_models = device.vendor_meta.get("models") if isinstance(device.vendor_meta, dict) else None
        if isinstance(meta_models, (list, tuple)):
            raw_candidates.extend(str(m) for m in meta_models)
        raw_candidates.extend(device.model_numbers)
        if not raw_candidates:
            raw_candidates.append(device.codename)

        models: list[str] = []
        for raw in raw_candidates:
            norm = normalize_oplus_model(raw)
            if norm and norm not in models:
                models.append(norm)
        return models

    @staticmethod
    def _build_ota_client(http: OfficialMetadataHTTP) -> OPlusOTAClient:
        raw_client = getattr(http, "client", None) or getattr(http, "_client", None)
        sleep_fn = getattr(http, "_sleep_fn", None)
        max_retries = getattr(http, "max_retries", 2)
        if callable(sleep_fn):
            return OPlusOTAClient(
                http_client=raw_client,
                max_retries=max_retries,
                sleep_fn=sleep_fn,
            )
        return OPlusOTAClient(http_client=raw_client, max_retries=max_retries)

    def fetch_releases(
        self,
        device: AdapterDevice,
        http: OfficialMetadataHTTP,
    ) -> list[AdapterRelease]:
        models = self._extract_device_models(device)
        if not models:
            return []

        ota_client = self._build_ota_client(http)
        os_name = VENDOR_OS_NAMES[self.vendor_id]
        releases: list[AdapterRelease] = []
        seen_identities: set[tuple[str, str, str]] = set()

        try:
            for model in models:
                target_regions = select_regions_for_model(model)
                for region in target_regions:
                    reg_cfg = REGION_CONFIGS.get(region)
                    if not reg_cfg:
                        continue
                    carriers = tuple(reg_cfg.get("carriers") or (reg_cfg["carrier_id"],))
                    pc_endpoint = str(reg_cfg["pc_url"])

                    for carrier in carriers:
                        for branch in self.branches:
                            seed_ver = zero_tail_seed_version(model, branch)
                            try:
                                packet = ota_client.query_update(
                                    model=model,
                                    ota_version=seed_ver,
                                    region=region,
                                    nv_carrier=carrier,
                                    use_pc_channel=True,
                                )
                            except httpx.HTTPStatusError as exc:
                                if exc.response.status_code in (400, 403, 404, 410):
                                    continue
                                raise

                            raw_best = (packet.manual_url or packet.auto_url or "").strip()
                            if not raw_best or not packet.is_full_ota:
                                continue

                            parsed_best = urlsplit(raw_best)
                            if parsed_best.path.endswith("/download"):
                                continue

                            raw_ver_candidate = (
                                (packet.version_name or "").strip()
                                or (packet.ota_target_version or "").strip()
                            )

                            if _is_oplus_gate_url(raw_best):
                                try:
                                    resolved_cdn = ota_client.resolve_download_check(raw_best)
                                except (OPlusGateError, httpx.HTTPError):
                                    continue

                                try:
                                    validated_cdn = validate_official_download_url(
                                        self.vendor_id,
                                        resolved_cdn,
                                        resolve_dns=False,
                                    )
                                except ValueError:
                                    continue

                                raw_fn = PurePosixPath(urlsplit(validated_cdn).path).name
                                version_name = raw_ver_candidate or raw_fn[:-4]
                                safe_fn = _sanitize_zip_filename(raw_fn, version_name or model)
                                if not safe_fn or not version_name:
                                    continue

                                try:
                                    refresh_source, refresh_url = _build_oplus_refresh_descriptor(
                                        vendor_id=self.vendor_id,
                                        version_name=version_name,
                                        gate_url=raw_best,
                                        filename=safe_fn,
                                    )
                                except ValueError:
                                    continue

                                canon_ver = canonical_version_str(version_name)
                                dedup_key = (model, region, canon_ver)
                                if dedup_key in seen_identities:
                                    continue
                                seen_identities.add(dedup_key)

                                delivery_mode = "resolver_oplus"
                                verification_status = "VERIFIED_RESOLVER"
                                official_url = refresh_url
                                fast_url = refresh_url
                                resolver_meta: dict[str, Any] | None = {"source": refresh_source}
                                filename = safe_fn
                            else:
                                clean_url = clean_oplus_cdn_url(raw_best)
                                raw_fn = unquote(PurePosixPath(urlsplit(clean_url).path).name)
                                if not raw_fn.lower().endswith(".zip"):
                                    continue
                                try:
                                    validated_direct = validate_official_download_url(
                                        self.vendor_id,
                                        clean_url,
                                        resolve_dns=False,
                                    )
                                except ValueError:
                                    continue

                                version_name = raw_ver_candidate or raw_fn[:-4]
                                if not version_name:
                                    continue

                                canon_ver = canonical_version_str(version_name)
                                dedup_key = (model, region, canon_ver)
                                if dedup_key in seen_identities:
                                    continue
                                seen_identities.add(dedup_key)

                                delivery_mode = "direct_cdn"
                                verification_status = "VERIFIED_IMMUTABLE"
                                official_url = validated_direct
                                fast_url = validated_direct
                                resolver_meta = None
                                filename = raw_fn

                            size_bytes = packet.size_bytes
                            size_str = format_bytes(size_bytes) if size_bytes is not None else None
                            sec_patch = _normalize_security_patch(packet.security_patch)
                            pub_date = _extract_ota_published_date(packet.ota_target_version)
                            android_ver = _normalize_android_version(packet.android_version)
                            os_ver = _normalize_os_version(packet.coloros_version)
                            checksum = _normalize_checksum(packet.md5)

                            releases.append(
                                AdapterRelease(
                                    device_codename=device.codename,
                                    model_code=model,
                                    region_code=region,
                                    version_str=canon_ver,
                                    android_version=android_ver,
                                    package_type="recovery_zip",
                                    original_official_url=official_url,
                                    fast_cdn_url=fast_url,
                                    md5_or_sha256=checksum,
                                    published_date=pub_date,
                                    file_size_bytes=size_bytes,
                                    file_size_str=size_str,
                                    delivery_mode=delivery_mode,
                                    verification_status=verification_status,
                                    resolver_meta=resolver_meta,
                                    display_version=version_name,
                                    raw_version=packet.ota_target_version or version_name,
                                    os_name=os_name,
                                    os_version=os_ver,
                                    security_patch=sec_patch,
                                    original_filename=filename,
                                    vendor_fields={
                                        "nv_carrier": packet.nv_carrier,
                                        "ota_target_version": packet.ota_target_version,
                                        "packet_type": packet.packet_type or "1",
                                    },
                                    sources={"oplus_ota_pc_v3": pc_endpoint},
                                    availability="available",
                                )
                            )
        finally:
            ota_client.close()

        return releases

    def resolve_delivery(
        self,
        release: AdapterRelease,
        http: OfficialMetadataHTTP,
    ) -> AdapterRelease:
        if (
            release.delivery_mode == "resolver_oplus"
            and isinstance(release.resolver_meta, dict)
            and isinstance(release.resolver_meta.get("source"), dict)
            and release.original_official_url.startswith(download_refresh.PREFIX)
        ):
            return release
        return release


__all__ = [
    "OPLUS_OTA_MODEL_RE",
    "OPlusAdapter",
    "SUPPORTED_OPLUS_VENDORS",
    "VENDOR_OS_NAMES",
    "format_bytes",
    "normalize_oplus_model",
    "select_regions_for_model",
]
