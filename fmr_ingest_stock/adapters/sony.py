"""Official Sony Xperia v4 stock firmware adapter.

Uses Sony's live ESS `public/api/product/v2` directory to stay synchronized with
official `ProductObjectId`, `HWVariantId`, `ModelObjectId`, and EOS metadata,
paired with `config/sony_customizations.yaml` for commercial `CDFId` and Gen2
`SonyProductCode` (`SPC`) seeds.

Official releases are assembled on demand by `fmr_ingest_stock.sony_bundle` from
Sony's signed ESS file chunks (`package_type="sony_bundle"`,
`delivery_mode="r2_bundle"`).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import datetime
from pathlib import Path
import re
import time
from typing import Any
from urllib.parse import urlencode, urlsplit
import httpx
import yaml

from fmr_core.publish_spec import canonical_version_str
from fmr_ingest_stock.device_identity import marketing_name as format_marketing_name
from fmr_ingest_stock.oem_sources import canonical_code
from fmr_ingest_stock.pipeline.verifier import format_file_size
from fmr_ingest_stock.sony_source import (
    API as SONY_ESS_API,
    api_url as validate_sony_api_url,
    field as xml_field,
    link as xml_link,
    manifest_digest,
    nodes as xml_nodes,
    validate_manifest,
    xml as parse_sony_xml,
)

from .base import AdapterDevice, AdapterRelease, OfficialMetadataHTTP, VendorAdapter
from .google_play_devices import _is_excluded_category

DEFAULT_SONY_CUSTOMIZATIONS_PATH = (
    Path(__file__).resolve().parents[2] / "config" / "sony_customizations.yaml"
)
SONY_PRODUCT_V2_URL = f"{SONY_ESS_API}/product/v2"
SONY_MATCH_V2_URL = f"{SONY_ESS_API}/device-service/match/v2"

_SERVICE_LOGIC_PRIORITY: tuple[str, ...] = (
    "SOFTWARE_UPDATE_CONTENT_ERASE",
    "CR_SOFTWARE_UPDATE",
    "SOFTWARE_UPDATE",
)
_RE_EXCLUDED_SONY_ACCESSORY = re.compile(
    r"\b(?:smartwatch|watch|band|data\s+transmitter|projector|ear(?:\s+duo)?)\b",
    re.IGNORECASE,
)
_RE_SAFE_FILENAME = re.compile(r"^[A-Za-z0-9._-]{1,180}$")
_RE_VERSION_SAFE = re.compile(r"^[A-Za-z0-9._/-]{1,64}$")
_MIN_VALID_VBMETA_EPOCH = 1_500_000_000  # 2017-07-14 UTC
_MAX_VALID_VBMETA_EPOCH = 2_500_000_000  # ~2049 UTC
_DEFAULT_REQUEST_DELAY_SECONDS = 0.05


def _default_sleep_fn(http: OfficialMetadataHTTP) -> Callable[[float], None]:
    if isinstance(getattr(http._client, "_transport", None), httpx.MockTransport):
        return lambda _: None
    return time.sleep


def _normalize_sony_region_code(cust_name: str, cust_id: str) -> str:
    """Build a unique, readable region/customization code per (model, CDFId)."""
    clean_name = re.sub(r"\s+", " ", (cust_name or "Customization")).replace("/", "-").strip()
    if len(clean_name) > 40:
        clean_name = clean_name[:40].rstrip()
    return f"{clean_name} · {cust_id}"


def _sanitize_service_source_url(service_url: str) -> str:
    """Strip transient Sony query tokens/passwords before storing provenance URL."""
    parts = urlsplit(service_url)
    return parts._replace(query="", fragment="").geturl()


def _parse_vbmeta_security_patch(root: Any) -> str | None:
    """Extract ISO `YYYY-MM-DD` security patch from Sony's `vbmeta` anti-rollback epoch."""
    for counter in xml_nodes(root, "anti-rollback-counter"):
        if xml_field(counter, "key").lower() != "vbmeta":
            continue
        raw_val = xml_field(counter, "value")
        if not raw_val.isdigit():
            continue
        ts = int(raw_val)
        if _MIN_VALID_VBMETA_EPOCH <= ts <= _MAX_VALID_VBMETA_EPOCH:
            return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).strftime("%Y-%m-%d")
    return None


def _normalize_sony_android_version(raw: str | None) -> str:
    val = (raw or "").strip()
    if re.fullmatch(r"\d+(?:\.\d+)*", val):
        return val
    return "unknown"


def _normalize_sony_version_str(raw_version: str) -> str:
    clean = re.sub(r"\s+", "", (raw_version or "").strip())
    if _RE_VERSION_SAFE.fullmatch(clean):
        return canonical_version_str(clean)
    cleaned = re.sub(r"[^A-Za-z0-9._/-]+", "-", clean).strip("-")
    if not cleaned:
        raise ValueError(f"Unusable Sony software-version: {raw_version!r}")
    return canonical_version_str(cleaned[:64])


def _safe_bundle_filename(model_name: str, version: str, cdf_id: str) -> str:
    candidate = re.sub(r"[^A-Za-z0-9._-]+", "_", f"Sony_{model_name}_{version}_CDF{cdf_id}.zip")
    if _RE_SAFE_FILENAME.fullmatch(candidate):
        return candidate
    return re.sub(r"[^A-Za-z0-9._-]+", "_", f"Sony_{model_name}_CDF{cdf_id}.zip")[:180]


def load_sony_customizations(
    path: str | Path = DEFAULT_SONY_CUSTOMIZATIONS_PATH,
) -> dict[str, dict[str, Any]]:
    """Load seeded Sony product labels & `(model -> customizations)` lookup."""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    products = raw.get("products") or {}
    out: dict[str, dict[str, Any]] = {}
    if not isinstance(products, Mapping):
        raise ValueError("sony_customizations.yaml must contain a 'products' mapping")
    for product_name, prod_data in products.items():
        if not isinstance(prod_data, Mapping):
            continue
        label = str(prod_data.get("label") or product_name).strip()
        device_type = str(prod_data.get("device_type") or "phone").strip().lower()
        year = prod_data.get("year")
        models = prod_data.get("models") or {}
        if not isinstance(models, Mapping):
            continue
        for model_name, cust_list in models.items():
            if not isinstance(cust_list, Sequence) or isinstance(cust_list, (str, bytes)):
                continue
            cleaned_custs: list[dict[str, str]] = []
            for item in cust_list:
                if not isinstance(item, Mapping):
                    continue
                cid = str(item.get("id") or "").strip()
                cname = str(item.get("name") or "Customization").strip()
                spc = str(item.get("spc") or "").strip()
                if not cid:
                    continue
                entry = {"id": cid, "name": cname or "Customization"}
                if spc:
                    entry["spc"] = spc
                cleaned_custs.append(entry)
            if cleaned_custs:
                out[str(model_name).strip()] = {
                    "product_name": str(product_name).strip(),
                    "label": label,
                    "device_type": device_type,
                    "year": int(year) if isinstance(year, int) else None,
                    "custs": cleaned_custs,
                }
    return out


class SonyAdapter(VendorAdapter):
    """Official Sony Xperia v4 adapter backed by ESS `product/v2` + seeded customizations."""

    vendor_id = "sony"

    def __init__(
        self,
        *,
        product_v2_url: str = SONY_PRODUCT_V2_URL,
        match_v2_url: str = SONY_MATCH_V2_URL,
        customizations_path: str | Path = DEFAULT_SONY_CUSTOMIZATIONS_PATH,
        request_delay_seconds: float = _DEFAULT_REQUEST_DELAY_SECONDS,
        max_custs_per_device: int | None = None,
        sleep_fn: Callable[[float], None] | None = None,
    ) -> None:
        self.product_v2_url = product_v2_url
        self.match_v2_url = match_v2_url
        self.customizations_path = Path(customizations_path)
        self.request_delay_seconds = max(0.0, float(request_delay_seconds))
        self.max_custs_per_device = (
            max(1, int(max_custs_per_device)) if max_custs_per_device is not None else None
        )
        self._sleep_fn = sleep_fn

    def discover_devices(self, http: OfficialMetadataHTTP) -> list[AdapterDevice]:
        seeds = load_sony_customizations(self.customizations_path)
        resp = http.get(self.product_v2_url)
        root = parse_sony_xml(resp.content)

        products_by_id: dict[str, str] = {}
        for prod in xml_nodes(root, "product"):
            pid = xml_field(prod, "id") or xml_field(prod, "Id")
            pname = xml_field(prod, "name")
            if pid:
                products_by_id[pid] = pname

        hw_to_pid: dict[str, str] = {}
        for hw in xml_nodes(root, "hwvariant"):
            hid = xml_field(hw, "Id") or xml_field(hw, "id")
            pid = xml_field(hw, "productId")
            if hid and pid:
                hw_to_pid[hid] = pid

        devices: list[AdapterDevice] = []
        seen_codenames: set[str] = set()

        for mod in xml_nodes(root, "model"):
            model_name = xml_field(mod, "name")
            mid = xml_field(mod, "Id") or xml_field(mod, "id")
            hwid = xml_field(mod, "hwVariantId")
            pid = hw_to_pid.get(hwid, "")
            if not (model_name and mid and hwid and pid):
                continue

            seed = seeds.get(model_name)
            if not seed:
                continue

            label = seed["label"]
            if _RE_EXCLUDED_SONY_ACCESSORY.search(f"{label} {model_name}"):
                continue
            if _is_excluded_category(
                marketing_name=label,
                codename=model_name.lower(),
                model=model_name,
                include_wearables=False,
            ):
                continue

            codename = canonical_code("sony", model_name)
            if codename in seen_codenames:
                continue
            seen_codenames.add(codename)

            clean_label = format_marketing_name(
                {
                    "vendor": "sony",
                    "upstream_id": codename,
                    "codename": codename,
                    "name": label,
                    "models": model_name,
                }
            )
            if not re.match(r"^Sony\b", clean_label, re.IGNORECASE):
                clean_label = f"Sony {clean_label}"

            year = seed.get("year")
            boot_type = "init_boot" if (isinstance(year, int) and year >= 2023) else "boot"
            eos = xml_field(mod, "isModelEOS").lower() == "true"

            devices.append(
                AdapterDevice(
                    codename=codename,
                    brand_id="sony",
                    marketing_name=clean_label,
                    model_numbers=(model_name,),
                    boot_partition_type=boot_type,
                    has_vbmeta=1,
                    vendor_meta={
                        "sony": {
                            "model": model_name,
                            "product_name": products_by_id.get(pid) or seed["product_name"],
                            "pid": pid,
                            "hwid": hwid,
                            "mid": mid,
                            "eos": eos,
                            "custs": seed["custs"],
                        }
                    },
                )
            )

        devices.sort(key=lambda d: d.codename)
        return devices

    def fetch_releases(
        self,
        device: AdapterDevice,
        http: OfficialMetadataHTTP,
    ) -> list[AdapterRelease]:
        meta = device.vendor_meta if isinstance(device.vendor_meta, dict) else {}
        config = meta.get("sony") if isinstance(meta.get("sony"), dict) else None
        if not config:
            return []

        pid = str(config.get("pid") or "").strip()
        hwid = str(config.get("hwid") or "").strip()
        mid = str(config.get("mid") or "").strip()
        model_name = str(config.get("model") or (device.model_numbers[0] if device.model_numbers else "")).strip()
        custs = list(config.get("custs") or ())
        if not (pid and hwid and mid and model_name and custs):
            return []

        if self.max_custs_per_device is not None:
            custs = custs[: self.max_custs_per_device]

        sleep_fn = self._sleep_fn or _default_sleep_fn(http)
        releases: list[AdapterRelease] = []
        seen_urls: set[str] = set()
        seen_identities: set[tuple[str, str, str]] = set()

        for idx, cust in enumerate(custs):
            if idx > 0 and self.request_delay_seconds > 0:
                sleep_fn(self.request_delay_seconds)

            cust_id = str(cust.get("id") or "").strip()
            cust_name = str(cust.get("name") or "Customization").strip()
            cust_spc = str(cust.get("spc") or "").strip()
            if not cust_id:
                continue

            rel = self._fetch_single_customization(
                http=http,
                device=device,
                model_name=model_name,
                pid=pid,
                hwid=hwid,
                mid=mid,
                cust_id=cust_id,
                cust_name=cust_name,
                cust_spc=cust_spc,
            )
            if rel is None:
                continue
            if rel.fast_cdn_url in seen_urls:
                continue
            ikey = (rel.model_code, rel.region_code, rel.canonical_version)
            if ikey in seen_identities:
                continue
            seen_urls.add(rel.fast_cdn_url)
            seen_identities.add(ikey)
            releases.append(rel)

        return releases

    def _fetch_single_customization(
        self,
        *,
        http: OfficialMetadataHTTP,
        device: AdapterDevice,
        model_name: str,
        pid: str,
        hwid: str,
        mid: str,
        cust_id: str,
        cust_name: str,
        cust_spc: str,
    ) -> AdapterRelease | None:
        query = {
            "CDFId": cust_id,
            "HwSetupKey": "Default",
            "HWVariantId": hwid,
            "ModelObjectId": mid,
            "ProductObjectId": pid,
            "SecurityStateType": "COMMERCIAL",
        }
        if cust_spc:
            query["SonyProductCode"] = cust_spc

        try:
            match_resp = http.get(f"{self.match_v2_url}?{urlencode(query)}")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (400, 403, 404, 410):
                return None
            raise

        match_root = parse_sony_xml(match_resp.content)
        services: list[dict[str, str | None]] = []
        for node in xml_nodes(match_root, "software-device-service-info"):
            sw_ver = xml_field(node, "software-version")
            logic = xml_field(node, "logic-type")
            raw_url = xml_link(node, "self")
            if not (sw_ver and logic and raw_url):
                continue
            services.append(
                {
                    "version": sw_ver,
                    "logic": logic,
                    "url": validate_sony_api_url(raw_url),
                    "android_version": xml_field(node, "android-version") or None,
                    "security_patch": _parse_vbmeta_security_patch(node),
                }
            )

        if not services:
            return None

        chosen = min(
            services,
            key=lambda s: _SERVICE_LOGIC_PRIORITY.index(s["logic"] or "")
            if s["logic"] in _SERVICE_LOGIC_PRIORITY
            else 99,
        )
        service_url = str(chosen["url"])
        try:
            listing_resp = http.get(service_url)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (400, 403, 404, 410):
                return None
            raise
        listing_root = parse_sony_xml(listing_resp.content)

        android_ver = _normalize_sony_android_version(
            xml_field(listing_root, "android-version") or chosen["android_version"]
        )
        security_patch = chosen["security_patch"] or _parse_vbmeta_security_patch(listing_root)

        files: list[dict[str, Any]] = []
        for node in xml_nodes(listing_root, "file-resource"):
            chunk_info_raw = xml_link(node, "file-chunk-info")
            if not chunk_info_raw:
                return None
            try:
                chunks_resp = http.get(validate_sony_api_url(chunk_info_raw))
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in (400, 403, 404, 410):
                    return None
                raise
            chunks_root = parse_sony_xml(chunks_resp.content)
            chunks: list[dict[str, Any]] = []
            for chunk in xml_nodes(chunks_root, "file-chunk"):
                dl_url = xml_link(chunk, "download")
                host = (urlsplit(dl_url).hostname or "").lower()
                if dl_url.startswith("http://") and (
                    host == "sonymobile.com"
                    or host.endswith(".sonymobile.com")
                    or host == "sony.net"
                    or host.endswith(".sony.net")
                ):
                    dl_url = "https://" + dl_url[7:]
                chunks.append(
                    {
                        "number": int(chunk.get("number", "0")),
                        "size": int(xml_field(chunk, "size")),
                        "url": dl_url,
                    }
                )
            chunks.sort(key=lambda c: c["number"])
            files.append(
                {
                    "key": xml_field(node, "file-key"),
                    "name": xml_field(node, "name"),
                    "size": int(xml_field(node, "plain-size")),
                    "chunks": chunks,
                }
            )

        sw_version = str(chosen["version"])
        manifest = {
            "model": model_name,
            "cust": cust_name,
            "cdf": cust_id,
            "version": sw_version,
            "service": service_url,
            "files": files,
        }
        try:
            total_bytes = validate_manifest(manifest)
        except ValueError:
            return None

        key = manifest_digest(manifest)
        bundle_url = f"https://findmyrom.com/sony/bundles/{key}"
        region_code = _normalize_sony_region_code(cust_name, cust_id)
        canon_ver = _normalize_sony_version_str(sw_version)

        vendor_fields: dict[str, Any] = {
            "cdf_id": cust_id,
            "customization": cust_name,
            "logic_type": str(chosen["logic"]),
            "component_count": len(files),
        }
        if cust_spc:
            vendor_fields["spc"] = cust_spc

        return AdapterRelease(
            device_codename=device.codename,
            model_code=model_name,
            region_code=region_code,
            version_str=canon_ver,
            android_version=android_ver,
            package_type="sony_bundle",
            original_official_url=bundle_url,
            fast_cdn_url=bundle_url,
            md5_or_sha256=None,
            published_date=None,
            file_size_bytes=total_bytes,
            file_size_str=format_file_size(total_bytes),
            delivery_mode="r2_bundle",
            verification_status="VERIFIED_R2_BUNDLE",
            resolver_meta=manifest,
            display_version=sw_version,
            raw_version=sw_version,
            security_patch=security_patch,
            original_filename=_safe_bundle_filename(model_name, sw_version, cust_id),
            vendor_fields=vendor_fields,
            sources={"sony_ess": _sanitize_service_source_url(service_url)},
            availability="available",
        )


__all__ = [
    "DEFAULT_SONY_CUSTOMIZATIONS_PATH",
    "SONY_MATCH_V2_URL",
    "SONY_PRODUCT_V2_URL",
    "SonyAdapter",
    "load_sony_customizations",
]
