"""Official Samsung Stock Firmware Adapter (Google Play CSV + FOTA version.xml + doc.samsungmobile.com + SmartFUS)."""

from __future__ import annotations

from collections.abc import Sequence
import re
import time
from typing import Any

import httpx

from fmr_core.publish_spec import canonical_version_str
from fmr_ingest_stock import download_refresh
from fmr_ingest_stock.device_identity import marketing_name as format_marketing_name
from fmr_ingest_stock.oem_sources import canonical_code
from fmr_ingest_stock.samsung_fus import (
    HOST as FUS_HOST,
    FUSAuthError,
    FirmwareUnavailable,
    SmartFUS,
    clear_fus_session_cookies,
    extract_binary_name_date,
    normalize_version,
    parse_version_xml,
)
from fmr_ingest_stock.samsung_metadata import fetch_model_csc_notices

from .base import (
    AdapterDevice,
    AdapterRelease,
    OfficialMetadataHTTP,
    VendorAdapter,
    validate_official_metadata_url,
)
from .google_play_devices import (
    GOOGLE_PLAY_DEVICES_URL,
    fetch_google_play_devices,
)

PRIMARY_CSCS: tuple[str, ...] = (
    "EUX",
    "XAA",
    "CHC",
    "TGY",
    "BRI",
    "KOO",
    "INS",
    "INU",
    "XID",
    "THL",
    "TUR",
    "SER",
    "ZTO",
    "XSA",
)

FOTA_VERSION_XML_URL_TEMPLATE = "https://fota-cloud-dn.ospserver.net/firmware/{csc}/{model}/version.xml"
FOTA_HEADERS: dict[str, str] = {"User-Agent": "Kies2.0_FUS"}

_RE_SAMSUNG_PHONE_TABLET_MODEL = re.compile(r"^SM-[A-Z0-9]{3,16}$")
_RE_EXCLUDED_SAMSUNG_ACCESSORY = re.compile(
    r"\b(?:watch|buds|gear|fit|ring|smarttag|tv|monitor|projector|dex)\b",
    re.IGNORECASE,
)


def format_bytes(size_bytes: int) -> str:
    """Format byte count into human-readable binary/decimal unit string."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    units = ("KB", "MB", "GB", "TB")
    val = float(size_bytes)
    for unit in units:
        val /= 1024.0
        if val < 1024.0 or unit == units[-1]:
            return f"{val:.2f} {unit}"
    return f"{size_bytes} B"


def _normalize_samsung_model(raw_model: str, marketing_label: str) -> str | None:
    """Normalize and filter Samsung model strings to official SM-* phones/tablets only."""
    cleaned = raw_model.strip().upper()
    if cleaned.endswith("/DS"):
        cleaned = cleaned[:-3].strip()
    if not _RE_SAMSUNG_PHONE_TABLET_MODEL.fullmatch(cleaned):
        return None
    # Filter out Galaxy Watch / Gear / Buds / Ring / Fit prefixes (SM-R*, SM-L*)
    if cleaned.startswith(("SM-R", "SM-L")):
        return None
    if _RE_EXCLUDED_SAMSUNG_ACCESSORY.search(f"{marketing_label} {raw_model}"):
        return None
    return cleaned


def _build_samsung_refresh_descriptor(
    *,
    model: str,
    csc: str,
    fus_info: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    """Build validated Samsung FUS refresh descriptor and content-addressed refresh URL."""
    builder = getattr(download_refresh, "samsung_source", None)
    if callable(builder):
        source = builder(
            model=model,
            region=csc,
            version=fus_info["version"],
            filename=fus_info["filename"],
            path=fus_info["path"],
            size=int(fus_info["size"]),
        )
    else:
        source = {
            "schema": 1,
            "vendor": "samsung",
            "model": model,
            "region": csc,
            "version": fus_info["version"],
            "filename": fus_info["filename"],
            "path": fus_info["path"],
            "size": int(fus_info["size"]),
        }
    hasher = getattr(download_refresh, "source_hash", download_refresh.fingerprint)
    digest = hasher(source)
    refresh_url = f"https://findmyrom.com/stock/refresh/{digest}"
    return source, refresh_url


def _clear_fus_session_cookies(client: httpx.Client) -> None:
    """Clear prior Samsung FUS session cookies (`SESSION`, `SCOUTER`, `JSESSIONID`) before starting a fresh nonce."""
    clear_fus_session_cookies(client)


def _fus_sleep_fn(client: httpx.Client) -> Any:
    if isinstance(getattr(client, "_transport", None), httpx.MockTransport):
        return lambda _: None
    return time.sleep


class SamsungAdapter(VendorAdapter):
    """100% official Samsung stock firmware adapter."""

    vendor_id = "samsung"

    def __init__(
        self,
        *,
        cscs: Sequence[str] = PRIMARY_CSCS,
        play_csv_url: str = GOOGLE_PLAY_DEVICES_URL,
        max_upgrades_per_csc: int = 0,
    ) -> None:
        cleaned_cscs = tuple(
            dict.fromkeys(c.strip().upper() for c in cscs if c and re.fullmatch(r"[A-Za-z0-9]{3}", c.strip()))
        )
        if not cleaned_cscs:
            raise ValueError("SamsungAdapter requires at least one valid 3-character CSC")
        self.cscs = cleaned_cscs
        self.play_csv_url = play_csv_url
        self.max_upgrades_per_csc = max(0, int(max_upgrades_per_csc))

    def discover_devices(self, http: OfficialMetadataHTTP) -> list[AdapterDevice]:
        records = fetch_google_play_devices(
            http,
            brands="samsung",
            include_wearables=False,
            url=self.play_csv_url,
        )

        models_by_code: dict[str, list[str]] = {}
        names_by_code: dict[str, list[str]] = {}
        raw_codenames_by_code: dict[str, list[str]] = {}

        for rec in records:
            sm_model = _normalize_samsung_model(rec.model, rec.marketing_name)
            if sm_model is None:
                continue
            code = canonical_code("samsung", rec.codename)
            if not code or code == "__global__":
                continue

            models_by_code.setdefault(code, [])
            names_by_code.setdefault(code, [])
            raw_codenames_by_code.setdefault(code, [])

            if sm_model not in models_by_code[code]:
                models_by_code[code].append(sm_model)
            if rec.marketing_name and rec.marketing_name not in names_by_code[code]:
                names_by_code[code].append(rec.marketing_name)
            if rec.codename not in raw_codenames_by_code[code]:
                raw_codenames_by_code[code].append(rec.codename)

        devices: list[AdapterDevice] = []
        for code in sorted(models_by_code):
            sm_models = models_by_code[code]
            if not sm_models:
                continue
            candidate_names = names_by_code.get(code, [])
            chosen_raw = candidate_names[0] if candidate_names else sm_models[0]
            for cand in candidate_names:
                if cand.upper() not in sm_models:
                    chosen_raw = cand
                    break

            clean_label = format_marketing_name(
                {
                    "vendor": "samsung",
                    "upstream_id": code,
                    "codename": code,
                    "name": chosen_raw,
                    "models": ",".join(sm_models),
                }
            )
            devices.append(
                AdapterDevice(
                    codename=code,
                    brand_id="samsung",
                    marketing_name=clean_label or f"Samsung {sm_models[0]}",
                    model_numbers=tuple(sm_models),
                    boot_partition_type="init_boot",
                    has_vbmeta=1,
                    vendor_meta={
                        "models": list(sm_models),
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

        models: list[str] = []
        for raw in raw_candidates:
            norm = _normalize_samsung_model(raw, device.marketing_name)
            if norm and norm not in models:
                models.append(norm)
        return models

    def _resolve_fus_build(
        self,
        *,
        http: OfficialMetadataHTTP,
        model: str,
        csc: str,
        norm_version: str,
    ) -> dict[str, Any] | None:
        validate_official_metadata_url(
            f"{FUS_HOST}NF_SmartDownloadGenerateNonce.do",
            allowed_hosts=http.allowed_hosts,
        )
        validate_official_metadata_url(
            f"{FUS_HOST}NF_SmartDownloadBinaryInform.do",
            allowed_hosts=http.allowed_hosts,
        )
        sleep_fn = _fus_sleep_fn(http._client)
        for attempt in range(2):
            _clear_fus_session_cookies(http._client)
            try:
                return SmartFUS(http._client, sleep_fn=sleep_fn).binary_info(
                    model, csc, norm_version
                )
            except FUSAuthError:
                if attempt == 1:
                    return None
            except (FirmwareUnavailable, ValueError, httpx.HTTPError):
                return None
        return None

    def fetch_releases(
        self,
        device: AdapterDevice,
        http: OfficialMetadataHTTP,
    ) -> list[AdapterRelease]:
        models = self._extract_device_models(device)
        if not models:
            return []

        releases: list[AdapterRelease] = []
        seen_identities: set[tuple[str, str, str]] = set()

        for model in models:
            for csc in self.cscs:
                version_xml_url = FOTA_VERSION_XML_URL_TEMPLATE.format(csc=csc, model=model)
                try:
                    xml_resp = http.get(version_xml_url, headers=FOTA_HEADERS)
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code in (400, 403, 404, 410):
                        continue
                    raise

                try:
                    latest_info, upgrade_versions = parse_version_xml(xml_resp.content)
                except ValueError:
                    continue

                candidates: list[tuple[str, str | None]] = []
                if latest_info is not None:
                    candidates.append(latest_info)
                if self.max_upgrades_per_csc > 0 and upgrade_versions:
                    for upg_ver in upgrade_versions[-self.max_upgrades_per_csc :]:
                        candidates.append((upg_ver, None))

                if not candidates:
                    continue

                # Fetch official doc.samsungmobile.com build notices once per active (model, csc)
                try:
                    notices = fetch_model_csc_notices(http, model, csc)
                except Exception:
                    notices = {}

                for norm_ver, xml_os in candidates:
                    dedup_key = (model, csc, norm_ver)
                    if dedup_key in seen_identities:
                        continue

                    fus_info = self._resolve_fus_build(
                        http=http,
                        model=model,
                        csc=csc,
                        norm_version=norm_ver,
                    )
                    if fus_info is None:
                        continue

                    seen_identities.add(dedup_key)
                    resolved_ver = fus_info["version"]
                    parts = resolved_ver.split("/")
                    ap = parts[0]
                    csc_ver = parts[1] if len(parts) > 1 else ap
                    cp = parts[2] if len(parts) > 2 else ap

                    notice = notices.get(ap) or {}
                    android_ver = notice.get("android") or xml_os or "unknown"
                    pub_date = notice.get("date") or extract_binary_name_date(fus_info.get("filename"))
                    sec_patch = notice.get("patch")

                    refresh_source, refresh_url = _build_samsung_refresh_descriptor(
                        model=model,
                        csc=csc,
                        fus_info=fus_info,
                    )
                    size_bytes = int(fus_info["size"])
                    sources_map: dict[str, str] = {"version_xml": version_xml_url}
                    if notice.get("source"):
                        sources_map["doc_notice"] = str(notice["source"])

                    releases.append(
                        AdapterRelease(
                            device_codename=device.codename,
                            model_code=model,
                            region_code=csc,
                            version_str=canonical_version_str(resolved_ver),
                            android_version=str(android_ver),
                            package_type="samsung_encrypted",
                            original_official_url=refresh_url,
                            fast_cdn_url=refresh_url,
                            published_date=pub_date,
                            file_size_bytes=size_bytes,
                            file_size_str=format_bytes(size_bytes),
                            delivery_mode="resolver_fus",
                            verification_status="VERIFIED_RESOLVER",
                            resolver_meta={"source": refresh_source},
                            display_version=ap,
                            raw_version=resolved_ver,
                            os_name="One UI",
                            security_patch=sec_patch,
                            original_filename=str(fus_info["filename"]),
                            vendor_fields={
                                "ap": ap,
                                "csc_version": csc_ver,
                                "cp": cp,
                            },
                            sources=sources_map,
                            availability="available",
                        )
                    )

        return releases

    def resolve_delivery(
        self,
        release: AdapterRelease,
        http: OfficialMetadataHTTP,
    ) -> AdapterRelease:
        if (
            release.delivery_mode == "resolver_fus"
            and isinstance(release.resolver_meta, dict)
            and isinstance(release.resolver_meta.get("source"), dict)
            and release.file_size_bytes is not None
        ):
            return release

        raw_ver = release.raw_version or release.version_str
        norm_ver = normalize_version(raw_ver)
        validate_official_metadata_url(
            f"{FUS_HOST}NF_SmartDownloadGenerateNonce.do",
            allowed_hosts=http.allowed_hosts,
        )
        validate_official_metadata_url(
            f"{FUS_HOST}NF_SmartDownloadBinaryInform.do",
            allowed_hosts=http.allowed_hosts,
        )
        _clear_fus_session_cookies(http._client)
        fus_info = SmartFUS(http._client, sleep_fn=_fus_sleep_fn(http._client)).binary_info(
            release.model_code,
            release.region_code,
            norm_ver,
        )
        refresh_source, refresh_url = _build_samsung_refresh_descriptor(
            model=release.model_code,
            csc=release.region_code,
            fus_info=fus_info,
        )
        size_bytes = int(fus_info["size"])
        pub_date = release.published_date or extract_binary_name_date(fus_info.get("filename"))

        return AdapterRelease(
            device_codename=release.device_codename,
            model_code=release.model_code,
            region_code=release.region_code,
            version_str=canonical_version_str(fus_info["version"]),
            android_version=release.android_version,
            package_type="samsung_encrypted",
            original_official_url=refresh_url,
            fast_cdn_url=refresh_url,
            md5_or_sha256=release.md5_or_sha256,
            published_date=pub_date,
            file_size_bytes=size_bytes,
            file_size_str=format_bytes(size_bytes),
            delivery_mode="resolver_fus",
            verification_status="VERIFIED_RESOLVER",
            resolver_meta={"source": refresh_source},
            display_version=release.display_version or fus_info["version"].split("/")[0],
            raw_version=fus_info["version"],
            os_name=release.os_name or "One UI",
            os_version=release.os_version,
            security_patch=release.security_patch,
            original_filename=str(fus_info["filename"]),
            vendor_fields=release.vendor_fields,
            sources=release.sources,
            availability="available",
        )


__all__ = [
    "FOTA_HEADERS",
    "FOTA_VERSION_XML_URL_TEMPLATE",
    "PRIMARY_CSCS",
    "SamsungAdapter",
    "format_bytes",
]
