"""Official ASUS Zenfone & ROG Phone Stock Firmware Adapter."""

from __future__ import annotations

import json
from pathlib import PurePosixPath
import re
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

import httpx

from fmr_core.publish_spec import canonical_version_str
from fmr_ingest_stock.cdn_rewriter import validate_official_download_url
from fmr_ingest_stock.device_identity import marketing_name as format_marketing_name
from fmr_ingest_stock.oem_sources import canonical_code
from fmr_ingest_stock.release_metadata import iso_date

from .base import AdapterDevice, AdapterRelease, OfficialMetadataHTTP, VendorAdapter

ASUS_PD_LEVEL_SERIES_URL = (
    "https://www.asus.com/support/api/product.asmx/GetPDLevel"
    "?website=global&type=1&typeid=1&productflag=0"
)
ASUS_PD_LEVEL_MODELS_TEMPLATE = (
    "https://www.asus.com/support/api/product.asmx/GetPDLevel"
    "?website=global&type=2&typeid={series_id}&productflag=1"
)
ASUS_ODIN_FILTER_URL_TEMPLATE = (
    "https://odinapi.asus.com/recent-data/apiv2/SeriesFilterResult"
    "?SystemCode=asus&WebsiteCode=global&ProductLevel1Code=mobile-handhelds"
    "&ProductLevel2Code=phones&PageSize=100&PageIndex={page}"
)
ASUS_PDBIOS_URL_TEMPLATE = (
    "https://www.asus.com/support/webapi/ProductV2/GetPDBIOS"
    "?website=global&model={slug}&pdid={pdid}&cpu=&LevelTagId=0"
)
ASUS_PDBIOS_HASHED_URL_TEMPLATE = (
    "https://www.asus.com/support/webapi/ProductV2/GetPDBIOS"
    "?website=global&model={slug}&pdhashedid={hashed_id}&cpu=&LevelTagId=0"
)

ASUS_DISCOVERY_HEADERS: dict[str, str] = {
    "Origin": "https://www.asus.com",
    "Referer": "https://www.asus.com/",
}
ASUS_PDBIOS_HEADERS: dict[str, str] = {
    "Referer": "https://www.asus.com/",
}
ASUS_CDN_BASE = "https://dlcdnets.asus.com"

_RE_HTML_TAGS = re.compile(r"<[^>]+>")
_RE_WHITESPACE = re.compile(r"\s+")
_RE_SLUG_NON_ALNUM = re.compile(r"[^A-Za-z0-9]+")
_RE_ROG_BRAND = re.compile(r"\bROG\b", re.IGNORECASE)

_RE_EXCLUDED_ASUS_DEVICE = re.compile(
    r"\b(?:"
    r"chromebook|chrome\s*os|chromebox|vivowatch|zenwatch|smartwatch"
    r"|rog\s+ally|aeroactive|kunai|gamepad|devilcase|bumper|adapter|charger"
    r"|power\s*bank|zenpower|padfone\s+station|dock|windows"
    r")\b",
    re.IGNORECASE,
)

_RE_NON_FIRMWARE_CATEGORY = re.compile(
    r"\b(?:utilit(?:y|ies)|driver|manual|document|faq|tool|apk)\b",
    re.IGNORECASE,
)
_RE_NON_FIRMWARE_FILE = re.compile(
    r"(?:"
    r"game\s*genie|gamegenie"
    r"|unlock\s*(?:device|tool|app|apk)"
    r"|\bapk\b"
    r"|usb\s*driver|flashtool|flash\s*tool"
    r"|pc\s*link|sync\s*tool|user\s*manual"
    r")",
    re.IGNORECASE,
)

_RE_HW_MODEL_CODE = re.compile(
    r"(?:ASUS[_-])?(?<![A-Za-z0-9])(AI\d{4}[A-Z0-9]{0,3}|Z[A-Z]\d{3}[A-Z]{1,3}|I\d{3}[A-Z0-9]{1,3})(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_RE_SKU_EXPLICIT = re.compile(
    r"\b(?:for\s+)?(WW|EU|JP|CN|TW|US)\s+SKU\b",
    re.IGNORECASE,
)
_RE_SKU_VERSION_PREFIX = re.compile(
    r"^Z?(WW|EU|JP|CN|TW|US)[-_.]",
    re.IGNORECASE,
)
_RE_SKU_TOKEN = re.compile(
    r"(?:^|[-_(\s])(WW|EU|JP|CN|TW|US)(?:[-_)\s]|$)",
    re.IGNORECASE,
)
_RE_SHA256 = re.compile(r"\b([0-9a-fA-F]{64})\b")
_RE_MD5 = re.compile(r"\b([0-9a-fA-F]{32})\b")
_RE_ANDROID_VER = re.compile(r"\bAndroid\s+(\d{1,2}(?:\.\d+)*L?)\b", re.IGNORECASE)
_RE_FILESIZE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([KMGT]?B(?:ytes)?)\s*$", re.IGNORECASE)


def build_asus_slug(name: str) -> str:
    """Build official ASUS GetPDBIOS model URL slug."""
    return _RE_SLUG_NON_ALNUM.sub("-", name).strip("-")


def _clean_product_text(raw: str) -> str:
    no_html = _RE_HTML_TAGS.sub(" ", raw)
    no_tm = no_html.replace("\u2122", "").replace("\u00ae", "").replace("&nbsp;", " ")
    return _RE_WHITESPACE.sub(" ", no_tm).strip()


def _parse_human_size(raw_size: Any) -> tuple[int | None, str | None]:
    if raw_size is None:
        return None, None
    text = str(raw_size).strip()
    if not text:
        return None, None
    if text.isdigit():
        num = int(text)
        if num <= 0:
            return None, None
        return num, f"{num / (1024 ** 3):.2f} GB" if num >= 1024**3 else f"{num / (1024 ** 2):.2f} MB"
    m = _RE_FILESIZE.match(text)
    if not m:
        return None, text
    val = float(m.group(1))
    unit = m.group(2).upper()
    mult_map = {
        "B": 1,
        "BYTES": 1,
        "KB": 1024,
        "KBYTES": 1024,
        "MB": 1024**2,
        "MBYTES": 1024**2,
        "GB": 1024**3,
        "GBYTES": 1024**3,
        "TB": 1024**4,
        "TBYTES": 1024**4,
    }
    mult = mult_map.get(unit)
    if mult is None:
        return None, text
    return int(round(val * mult)), text


def _extract_result_items(payload: Any, keys: tuple[str, ...]) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    result = payload.get("Result")
    if isinstance(result, list):
        return [item for item in result if isinstance(item, dict)]
    if not isinstance(result, dict):
        return []

    # Official GetPDLevel(type=1) nests series categories inside Result.ProductLevel.{Products,Commercial}.Items
    # while leaving Result.Product == []! Collect non-empty nested ProductLevel items first if requested.
    if "ProductLevel" in keys:
        pl = result.get("ProductLevel")
        if isinstance(pl, dict):
            nested_items: list[dict[str, Any]] = []
            for group_key in ("Products", "Commercial"):
                grp = pl.get(group_key)
                if isinstance(grp, dict):
                    raw_items = grp.get("Items")
                    if isinstance(raw_items, list):
                        nested_items.extend(it for it in raw_items if isinstance(it, dict))
                elif isinstance(grp, list):
                    nested_items.extend(it for it in grp if isinstance(it, dict))
            if nested_items:
                return nested_items

    for k in keys:
        if k == "ProductLevel":
            continue
        arr = result.get(k)
        if isinstance(arr, list) and arr:
            return [item for item in arr if isinstance(item, dict)]
    return []


def _extract_hardware_code(*candidates: str, fallback: str) -> str:
    for text in candidates:
        if not text:
            continue
        m = _RE_HW_MODEL_CODE.search(text)
        if m:
            return m.group(1).upper()
    for text in candidates:
        cleaned = re.sub(r"^ASUS[_-]+", "", (text or "").strip(), flags=re.IGNORECASE)
        if cleaned and re.fullmatch(r"[A-Za-z0-9_-]{3,24}", cleaned):
            return cleaned.upper()
    return fallback


def _extract_sku_region(
    *,
    version: str,
    title: str,
    description: str,
    filename: str,
    china_only: bool,
) -> str:
    for text in (title, description):
        m = _RE_SKU_EXPLICIT.search(text)
        if m:
            return m.group(1).upper()
    m_ver = _RE_SKU_VERSION_PREFIX.match(version)
    if m_ver:
        return m_ver.group(1).upper()
    for text in (filename, title):
        m_tok = _RE_SKU_TOKEN.search(text)
        if m_tok:
            return m_tok.group(1).upper()
    if china_only:
        return "CN"
    return "Global"


def _extract_sha256_or_md5(file_entry: dict[str, Any], description: str, title: str) -> str | None:
    direct_keys = ("Sha256", "SHA256", "sha256", "FileHash", "CheckSum", "Checksum", "MD5", "Md5")
    for k in direct_keys:
        raw = file_entry.get(k)
        if isinstance(raw, str) and raw.strip():
            m64 = _RE_SHA256.search(raw.strip())
            if m64:
                return m64.group(1).lower()
            m32 = _RE_MD5.search(raw.strip())
            if m32:
                return m32.group(1).lower()
    for text in (description, title):
        if not text:
            continue
        m64 = _RE_SHA256.search(text)
        if m64:
            return m64.group(1).lower()
    for text in (description, title):
        if not text:
            continue
        m32 = _RE_MD5.search(text)
        if m32:
            return m32.group(1).lower()
    return None


class AsusAdapter(VendorAdapter):
    """100% official ASUS Zenfone & ROG Phone stock firmware adapter."""

    vendor_id = "asus"

    def __init__(self, *, max_odin_pages: int = 5) -> None:
        self.max_odin_pages = max(1, int(max_odin_pages))

    def _collect_pd_level_models(self, http: OfficialMetadataHTTP) -> list[dict[str, str | None]]:
        discovered: list[dict[str, str | None]] = []
        series_resp = http.get(ASUS_PD_LEVEL_SERIES_URL, headers=ASUS_DISCOVERY_HEADERS)
        series_payload = json.loads(series_resp.text)
        series_items = _extract_result_items(
            series_payload, ("ProductLevel", "Product", "Obj", "Series")
        )

        seen_series: set[str] = set()
        for s_item in series_items:
            s_name = _clean_product_text(
                str(s_item.get("Name") or s_item.get("PDName") or s_item.get("SeriesName") or "")
            )
            if s_name and _RE_EXCLUDED_ASUS_DEVICE.search(s_name):
                continue
            s_id = str(
                s_item.get("Id")
                or s_item.get("ID")
                or s_item.get("PDLevelId")
                or s_item.get("TypeId")
                or s_item.get("SeriesId")
                or ""
            ).strip()
            if not s_id or not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", s_id) or s_id in seen_series:
                continue
            seen_series.add(s_id)

            models_url = ASUS_PD_LEVEL_MODELS_TEMPLATE.format(series_id=quote(s_id, safe=""))
            try:
                models_resp = http.get(models_url, headers=ASUS_DISCOVERY_HEADERS)
                models_payload = json.loads(models_resp.text)
            except (httpx.HTTPStatusError, ValueError):
                continue

            for m_item in _extract_result_items(models_payload, ("Product", "Obj", "Models")):
                pdid = str(
                    m_item.get("PDId")
                    or m_item.get("PDID")
                    or m_item.get("Id")
                    or m_item.get("ID")
                    or m_item.get("RealProductID")
                    or ""
                ).strip()
                name = _clean_product_text(
                    str(
                        m_item.get("PDName")
                        or m_item.get("Name")
                        or m_item.get("ProductName")
                        or m_item.get("ModelName")
                        or ""
                    )
                )
                hashed_id = (
                    str(
                        m_item.get("PDHashedId")
                        or m_item.get("HashedId")
                        or m_item.get("HashedID")
                        or m_item.get("HashId")
                        or ""
                    ).strip()
                    or None
                )
                if pdid and name:
                    discovered.append(
                        {
                            "pdid": pdid,
                            "name": name,
                            "hashed_id": hashed_id,
                            "spec_model": None,
                        }
                    )
        return discovered

    def _collect_odin_models(self, http: OfficialMetadataHTTP) -> list[dict[str, str | None]]:
        discovered: list[dict[str, str | None]] = []
        for page in range(1, self.max_odin_pages + 1):
            odin_url = ASUS_ODIN_FILTER_URL_TEMPLATE.format(page=page)
            try:
                resp = http.get(odin_url, headers=ASUS_DISCOVERY_HEADERS)
                payload = json.loads(resp.text)
            except (httpx.HTTPStatusError, ValueError):
                break

            items = _extract_result_items(
                payload,
                ("ProductList", "Obj", "Products", "Items", "List"),
            )
            if not items:
                break

            for item in items:
                pdid = str(
                    item.get("RealProductID")
                    or item.get("PDId")
                    or item.get("PDID")
                    or item.get("Id")
                    or ""
                ).strip()
                name = _clean_product_text(
                    str(
                        item.get("ProductName")
                        or item.get("PDName")
                        or item.get("Name")
                        or item.get("Title")
                        or ""
                    )
                )
                hashed_id = (
                    str(
                        item.get("ProductID")
                        or item.get("PDHashedId")
                        or item.get("HashedId")
                        or ""
                    ).strip()
                    or None
                )
                spec_model = str(item.get("ModelSpecName") or "").strip() or None
                if pdid and name:
                    discovered.append(
                        {
                            "pdid": pdid,
                            "name": name,
                            "hashed_id": hashed_id,
                            "spec_model": spec_model,
                        }
                    )

            result_obj = payload.get("Result") if isinstance(payload, dict) else None
            total_count = 0
            if isinstance(result_obj, dict):
                try:
                    total_count = int(result_obj.get("TotalCount") or 0)
                except (TypeError, ValueError):
                    total_count = 0
            if len(items) < 100 or (total_count > 0 and page * 100 >= total_count):
                break
        return discovered

    def discover_devices(self, http: OfficialMetadataHTTP) -> list[AdapterDevice]:
        raw_entries = [
            *self._collect_pd_level_models(http),
            *self._collect_odin_models(http),
        ]

        # Group cleanly: deduplicate by pdid first, then unify identical canonical codenames.
        codename_by_pdid: dict[str, str] = {}
        state_by_code: dict[str, dict[str, Any]] = {}

        for entry in raw_entries:
            pdid = entry["pdid"] or ""
            raw_name = entry["name"] or ""
            if not pdid or not raw_name or not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", pdid):
                continue
            if _RE_EXCLUDED_ASUS_DEVICE.search(raw_name):
                continue

            slug = build_asus_slug(raw_name)
            if not slug:
                continue

            brand_id = "rog" if _RE_ROG_BRAND.search(raw_name) else "asus"

            # Extract any embedded hardware model numbers (e.g. AI2401, ZS673KS)
            hw_models: list[str] = []
            for m_hw in _RE_HW_MODEL_CODE.finditer(f"{raw_name} {entry.get('spec_model') or ''}"):
                hw = m_hw.group(1).upper()
                if hw not in hw_models:
                    hw_models.append(hw)

            # Strip leading redundant "ASUS " and trailing "(AI2401)" for canonical codename stability
            base_for_code = re.sub(r"^ASUS\s+", "", raw_name, flags=re.IGNORECASE)
            base_for_code = re.sub(
                r"\s*\(\s*(?:ASUS[_-])?(?:AI\d{4}[A-Z0-9]{0,3}|Z[A-Z]\d{3}[A-Z]{1,3}|I\d{3}[A-Z0-9]{1,3})\s*\)",
                "",
                base_for_code,
                flags=re.IGNORECASE,
            ).strip()
            code_seed = build_asus_slug(base_for_code or raw_name).lower()
            if pdid in codename_by_pdid:
                code = codename_by_pdid[pdid]
            else:
                code = canonical_code(brand_id, code_seed)
                if not code or code == "__global__":
                    continue
                codename_by_pdid[pdid] = code

            display_label = format_marketing_name(
                {
                    "vendor": brand_id,
                    "upstream_id": code,
                    "codename": code,
                    "name": raw_name,
                    "models": ",".join(hw_models),
                }
            )

            if code not in state_by_code:
                state_by_code[code] = {
                    "brand_id": brand_id,
                    "marketing_name": display_label or raw_name,
                    "pdid": pdid,
                    "slug": slug,
                    "hashed_id": entry.get("hashed_id"),
                    "models": list(hw_models),
                    "targets": [
                        {
                            "pdid": pdid,
                            "slug": slug,
                            "hashed_id": entry.get("hashed_id"),
                        }
                    ],
                }
            else:
                st = state_by_code[code]
                if brand_id == "rog":
                    st["brand_id"] = "rog"
                if not st.get("hashed_id") and entry.get("hashed_id"):
                    st["hashed_id"] = entry["hashed_id"]
                for hw in hw_models:
                    if hw not in st["models"]:
                        st["models"].append(hw)
                existing_pdids = {t["pdid"] for t in st["targets"]}
                if pdid not in existing_pdids:
                    st["targets"].append(
                        {
                            "pdid": pdid,
                            "slug": slug,
                            "hashed_id": entry.get("hashed_id"),
                        }
                    )

        devices: list[AdapterDevice] = []
        for code in sorted(state_by_code):
            st = state_by_code[code]
            vendor_meta: dict[str, Any] = {
                "pdid": st["pdid"],
                "slug": st["slug"],
                "hashed_id": st["hashed_id"],
                "targets": st["targets"],
            }
            devices.append(
                AdapterDevice(
                    codename=code,
                    brand_id=st["brand_id"],
                    marketing_name=st["marketing_name"],
                    model_numbers=tuple(st["models"]),
                    boot_partition_type="init_boot",
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
        meta = device.vendor_meta or {}
        raw_targets = meta.get("targets")
        targets: list[dict[str, Any]] = []
        if isinstance(raw_targets, list) and raw_targets:
            for t in raw_targets:
                if isinstance(t, dict) and t.get("pdid") and t.get("slug"):
                    targets.append(t)
        elif meta.get("pdid") and meta.get("slug"):
            targets.append({"pdid": meta["pdid"], "slug": meta["slug"]})

        if not targets:
            return []

        releases: list[AdapterRelease] = []
        seen_urls: set[str] = set()

        for target in targets:
            pdid = str(target["pdid"]).strip()
            slug = str(target["slug"]).strip()
            hashed_id = str(target.get("hashed_id") or "").strip()
            pdbios_url = ASUS_PDBIOS_URL_TEMPLATE.format(
                slug=quote(slug, safe="-_."),
                pdid=quote(pdid, safe=""),
            )

            try:
                resp = http.get(pdbios_url, headers=ASUS_PDBIOS_HEADERS)
                payload = json.loads(resp.text)
            except (httpx.HTTPStatusError, ValueError):
                payload = None

            result = payload.get("Result") if isinstance(payload, dict) else None
            objs = result.get("Obj") if isinstance(result, dict) else None
            if (not isinstance(objs, list) or not objs) and hashed_id and re.fullmatch(
                r"[A-Za-z0-9_-]{4,64}", hashed_id
            ):
                hashed_url = ASUS_PDBIOS_HASHED_URL_TEMPLATE.format(
                    slug=quote(slug, safe="-_."),
                    hashed_id=quote(hashed_id, safe=""),
                )
                try:
                    resp_h = http.get(hashed_url, headers=ASUS_PDBIOS_HEADERS)
                    payload_h = json.loads(resp_h.text)
                except (httpx.HTTPStatusError, ValueError):
                    payload_h = None
                res_h = payload_h.get("Result") if isinstance(payload_h, dict) else None
                objs_h = res_h.get("Obj") if isinstance(res_h, dict) else None
                if isinstance(res_h, dict) and isinstance(objs_h, list) and objs_h:
                    result = res_h
                    objs = objs_h
                    pdbios_url = hashed_url

            if not isinstance(result, dict) or not isinstance(objs, list):
                continue

            result_model = str(result.get("Model") or "").strip()

            for obj in objs:
                if not isinstance(obj, dict):
                    continue
                cat_name = str(obj.get("Name") or "").strip()
                if cat_name and _RE_NON_FIRMWARE_CATEGORY.search(cat_name):
                    continue

                files = obj.get("Files")
                if not isinstance(files, list):
                    continue

                for file_entry in files:
                    if not isinstance(file_entry, dict):
                        continue

                    title = _clean_product_text(str(file_entry.get("Title") or ""))
                    raw_desc = str(file_entry.get("Description") or "")
                    clean_desc = _clean_product_text(raw_desc)

                    if _RE_NON_FIRMWARE_FILE.search(title):
                        continue

                    dl_field = file_entry.get("DownloadUrl")
                    raw_url = ""
                    china_only = False
                    if isinstance(dl_field, dict):
                        global_url = str(dl_field.get("Global") or "").strip()
                        china_url = str(dl_field.get("China") or "").strip()
                        raw_url = global_url or china_url
                        china_only = not bool(global_url) and bool(china_url)
                    elif isinstance(dl_field, str):
                        raw_url = dl_field.strip()

                    if not raw_url:
                        continue

                    if raw_url.startswith("//"):
                        resolved_url = f"https:{raw_url}"
                    elif raw_url.startswith("/"):
                        resolved_url = urljoin(ASUS_CDN_BASE, raw_url)
                    else:
                        resolved_url = raw_url

                    resolved_url = resolved_url.replace(" ", "%20")
                    parsed = urlsplit(resolved_url)
                    filename = PurePosixPath(parsed.path).name
                    if not filename or not filename.lower().endswith(".zip"):
                        continue
                    if _RE_NON_FIRMWARE_FILE.search(filename):
                        continue

                    try:
                        validated_url = validate_official_download_url(
                            device.brand_id,
                            resolved_url,
                            resolve_dns=False,
                        )
                    except ValueError:
                        continue

                    if validated_url in seen_urls:
                        continue

                    raw_version = str(file_entry.get("Version") or "").strip()
                    raw_version = re.sub(r"^Version\s+", "", raw_version, flags=re.IGNORECASE).strip()
                    if not raw_version:
                        m_fn_ver = re.search(
                            r"-(\d{2}\.\d{3,5}\.\d{3,5}\.\d{1,5})-",
                            filename,
                        )
                        if m_fn_ver:
                            raw_version = m_fn_ver.group(1)
                    if not raw_version:
                        continue

                    hw_code = _extract_hardware_code(
                        result_model,
                        title,
                        filename,
                        *(device.model_numbers or ()),
                        fallback=device.codename,
                    )
                    sku_region = _extract_sku_region(
                        version=raw_version,
                        title=title,
                        description=clean_desc,
                        filename=filename,
                        china_only=china_only,
                    )
                    pub_date = iso_date(file_entry.get("ReleaseDate"))
                    size_bytes, size_str = _parse_human_size(file_entry.get("FileSize"))
                    checksum = _extract_sha256_or_md5(file_entry, clean_desc, title)

                    m_android = _RE_ANDROID_VER.search(f"{title} {clean_desc}")
                    android_ver = m_android.group(1) if m_android else "unknown"

                    seen_urls.add(validated_url)
                    releases.append(
                        AdapterRelease(
                            device_codename=device.codename,
                            model_code=hw_code,
                            region_code=sku_region,
                            version_str=canonical_version_str(raw_version),
                            android_version=android_ver,
                            package_type="recovery_zip",
                            original_official_url=validated_url,
                            fast_cdn_url=validated_url,
                            md5_or_sha256=checksum,
                            published_date=pub_date,
                            file_size_bytes=size_bytes,
                            file_size_str=size_str,
                            delivery_mode="direct_cdn",
                            verification_status="VERIFIED_IMMUTABLE",
                            display_version=raw_version,
                            raw_version=raw_version,
                            os_name="ROG UI" if device.brand_id == "rog" else "ZenUI",
                            original_filename=filename,
                            vendor_fields={
                                "pdid": pdid,
                                "slug": slug,
                                "sku": sku_region,
                                "title": title or None,
                            },
                            sources={"asus_pdbios": pdbios_url},
                            availability="available",
                        )
                    )

        return releases


__all__ = [
    "ASUS_CDN_BASE",
    "ASUS_ODIN_FILTER_URL_TEMPLATE",
    "ASUS_PDBIOS_URL_TEMPLATE",
    "ASUS_PD_LEVEL_MODELS_TEMPLATE",
    "ASUS_PD_LEVEL_SERIES_URL",
    "AsusAdapter",
    "build_asus_slug",
]
