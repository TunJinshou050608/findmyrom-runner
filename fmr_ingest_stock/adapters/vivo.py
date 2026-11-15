"""Official vivo / iQOO Stock Firmware Adapter."""

from __future__ import annotations

from collections.abc import Sequence
import datetime
from pathlib import PurePosixPath
import re
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup
import httpx

from fmr_core.publish_spec import canonical_version_str
from fmr_ingest_stock.cdn_rewriter import validate_official_download_url
from fmr_ingest_stock.device_identity import marketing_name as format_marketing_name
from fmr_ingest_stock.oem_sources import canonical_code
from fmr_ingest_stock.release_metadata import iso_date

from .base import AdapterDevice, AdapterRelease, OfficialMetadataHTTP, VendorAdapter

DEFAULT_VIVO_REGIONS: tuple[str, ...] = (
    "in",
    "id",
    "my",
    "th",
    "ph",
    "tw",
    "hk",
    "vn",
    "sg",
    "ru",
    "eg",
    "bd",
    "pk",
    "ae",
    "kz",
    "np",
    "lk",
    "mm",
    "kh",
    "fr",
    "sa",
    "eu",
    "de",
    "es",
    "it",
    "pt",
    "nl",
    "pl",
    "cz",
    "gr",
    "hr",
    "rs",
    "bg",
    "ro",
    "si",
    "tr",
    "ua",
    "by",
    "az",
    "kg",
    "tj",
    "uz",
    "il",
    "bh",
    "iq",
    "jo",
    "kw",
    "lb",
    "om",
    "qa",
    "ye",
    "dz",
    "ma",
    "tn",
    "ng",
    "ke",
    "tz",
    "za",
    "ao",
    "au",
    "bt",
    "la",
    "pg",
    "cl",
    "co",
    "mx",
    "pe",
)
VIVO_PORTAL_LOCALE_PATHS: dict[str, str] = {
    "ae": "ae/en",
    "bh": "bh/ar",
    "eg": "eg/ar",
    "hk": "hk/zh",
    "iq": "iq/ar",
    "jo": "jo/ar",
    "ke": "ke/en",
    "kw": "kw/ar",
    "lb": "lb/ar",
    "ng": "ng/en",
    "om": "om/ar",
    "qa": "qa/ar",
    "sa": "sa/en",
    "tz": "tz/en",
    "ua": "ua/uk",
    "ye": "ye/en",
    "za": "za/en",
}
VIVO_HOME_URL_TEMPLATE = "https://www.vivo.com/{region}/support/upgradePackageHome"
VIVO_DATA_URL_TEMPLATE = "https://www.vivo.com/{region}/support/upgradePackageData?id={model_id}"
_VIVO_BROWSER_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )
}

ALLOWED_VIVO_ZIP_DOMAINS: tuple[str, ...] = (
    "vivoglobal.com",
    "vivo.com",
    "vivo.com.cn",
)

_RE_WHITESPACE = re.compile(r"\s+")
_RE_SLUG_NON_ALNUM = re.compile(r"[^A-Za-z0-9]+")
_RE_PD_MODEL = re.compile(r"(?<![A-Za-z0-9])(PD\d{4}[A-Z]{0,4})(?![A-Za-z0-9])", re.IGNORECASE)
_RE_VIVO_BUILD = re.compile(r"(?<![A-Za-z0-9])(PD\d{4}[A-Z0-9_.-]{6,80})(?![A-Za-z0-9])", re.IGNORECASE)
_RE_DATE_CANDIDATE = re.compile(r"\b(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})\b")
_RE_SIZE_CANDIDATE = re.compile(r"\b(\d+(?:\.\d+)?)\s*([MG]B|M|G)\b", re.IGNORECASE)
_RE_ANDROID_TEXT = re.compile(r"\bAndroid\s*(\d{1,2}(?:\.\d+)*L?)\b", re.IGNORECASE)
_RE_FUNTOUCH_TEXT = re.compile(r"\bFuntouch\s*OS\s*(\d+(?:\.\d+)*)\b", re.IGNORECASE)
_RE_ORIGINOS_TEXT = re.compile(r"\bOrigin\s*OS\s*(\d+(?:\.\d+)*)\b", re.IGNORECASE)
_RE_VERSION_ANDROID_MAJOR = re.compile(r"_(?:EX_)?A_(\d{2})\.", re.IGNORECASE)
_RE_LABELED_HASH = re.compile(
    r"\b(?:SHA-?256|MD5)\s*[:：]?\s*([0-9a-fA-F]{64}|[0-9a-fA-F]{32})\b",
    re.IGNORECASE,
)
_RE_TRAILING_UI_BADGES = re.compile(r"(?i)(?:\s+(?:new|hot|latest))+\s*$")
_RE_EXCLUDED_VIVO_ACCESSORY = re.compile(
    r"\b(?:tws|earbuds?|watch|band|charger)\b",
    re.IGNORECASE,
)
_RE_NON_FIRMWARE_VIVO_ZIP = re.compile(
    r"(?i)(?:log_aces|aces_profile|(?:^|[_ -])lut(?:[_ .-]|$)|camera_profile|usb_driver|(?:^|[_ -])driver|flashtool|af_tool|manual|user_guide)"
)


def _strip_vivo_ui_badges(text: str) -> str:
    return _RE_TRAILING_UI_BADGES.sub("", (text or "").strip()).strip()


def _is_non_firmware_vivo_zip(filename: str) -> bool:
    decoded = unquote(filename or "").strip()
    if not decoded:
        return True
    return _RE_NON_FIRMWARE_VIVO_ZIP.search(decoded) is not None


def _matches_vivo_zip_domain(host_lower: str) -> bool:
    return any(
        host_lower == dom or host_lower.endswith("." + dom)
        for dom in ALLOWED_VIVO_ZIP_DOMAINS
    )


def _normalize_vivo_zip_url(raw_href: str, base_url: str) -> str | None:
    cleaned = (raw_href or "").strip()
    if not cleaned or cleaned.startswith(("javascript:", "#", "mailto:")):
        return None
    if cleaned.startswith("//"):
        resolved = "https:" + cleaned
    elif cleaned.startswith(("http://", "https://")):
        resolved = cleaned
    else:
        resolved = urljoin(base_url, cleaned)

    resolved = resolved.replace(" ", "%20")
    parsed = urlsplit(resolved)
    if not parsed.hostname:
        return None
    host_lower = parsed.hostname.lower()
    if not _matches_vivo_zip_domain(host_lower):
        return None

    path_lower = (parsed.path or "").lower()
    if not path_lower.endswith(".zip"):
        return None
    filename = PurePosixPath(parsed.path or "").name
    if _is_non_firmware_vivo_zip(filename):
        return None

    # Upgrade legacy http:// vivo CDN links to https:// before strict validation
    https_url = urlunsplit(("https", host_lower, parsed.path, parsed.query, ""))
    try:
        return validate_official_download_url("vivo", https_url, resolve_dns=False)
    except ValueError:
        return None


def _extract_release_date(text: str) -> str | None:
    # Prefer labeled date fields first
    labeled = re.search(
        r"(?:Release\s*date|Update\s*(?:date|time)|Publish\s*date)\s*[:：]?\s*(20\d{2}[-/.]\d{1,2}[-/.]\d{1,2})",
        text,
        re.IGNORECASE,
    )
    candidates = []
    if labeled:
        candidates.append(labeled.group(1))
    for m in _RE_DATE_CANDIDATE.finditer(text):
        candidates.append(f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}")

    for raw in candidates:
        normalized = raw.replace(".", "-").replace("/", "-")
        parts = normalized.split("-")
        if len(parts) == 3:
            try:
                dt = datetime.date(int(parts[0]), int(parts[1]), int(parts[2]))
                if 2012 <= dt.year <= 2099:
                    return dt.isoformat()
            except ValueError:
                pass
        parsed = iso_date(raw)
        if parsed:
            return parsed
    return None


def _extract_package_size(text: str) -> tuple[int | None, str | None]:
    labeled = re.search(
        r"(?:File\s*size|Package\s*size|Size)\s*[:：]?\s*(\d+(?:\.\d+)?)\s*([MG]B|M|G)\b",
        text,
        re.IGNORECASE,
    )
    m = labeled or _RE_SIZE_CANDIDATE.search(text)
    if not m:
        return None, None
    val = float(m.group(1))
    unit_raw = m.group(2).upper()
    if unit_raw.startswith("G"):
        size_bytes = int(round(val * (1024**3)))
        return size_bytes, f"{val:.2f} GB" if "." in m.group(1) else f"{m.group(1)} GB"
    size_bytes = int(round(val * (1024**2)))
    return size_bytes, f"{val:.2f} MB" if "." in m.group(1) else f"{m.group(1)} MB"


def _extract_version_from_filename(filename: str) -> str | None:
    stem = filename[:-4] if filename.lower().endswith(".zip") else filename
    m_full = re.match(r"^([A-Za-z0-9_.-]+?)-update-full(?:_.*)?$", stem, re.IGNORECASE)
    if m_full:
        return m_full.group(1)
    m_pd = _RE_VIVO_BUILD.search(stem)
    if m_pd:
        return m_pd.group(1)
    return stem if re.fullmatch(r"[A-Za-z0-9_.-]{4,80}", stem) else None


class VivoAdapter(VendorAdapter):
    """100% official vivo / iQOO stock firmware adapter."""

    vendor_id = "vivo"

    def __init__(self, *, regions: Sequence[str] = DEFAULT_VIVO_REGIONS) -> None:
        cleaned = tuple(
            dict.fromkeys(
                r.strip().lower()
                for r in regions
                if r and re.fullmatch(r"[a-z]{2,8}", r.strip().lower())
            )
        )
        if not cleaned:
            raise ValueError("VivoAdapter requires at least one valid portal region code")
        self.regions = cleaned

    def discover_devices(self, http: OfficialMetadataHTTP) -> list[AdapterDevice]:
        state_by_code: dict[str, dict[str, Any]] = {}

        for region in self.regions:
            portal_path = VIVO_PORTAL_LOCALE_PATHS.get(region, region)
            home_url = VIVO_HOME_URL_TEMPLATE.format(region=portal_path)
            try:
                resp = http.get(home_url, headers=_VIVO_BROWSER_HEADERS)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in (400, 403, 404, 410, 418, 451):
                    continue
                raise

            soup = BeautifulSoup(resp.text, "html.parser")
            for anchor in soup.find_all("a", href=True):
                raw_href = str(anchor.get("href") or "").strip()
                if "upgradePackageData" not in raw_href:
                    continue
                resolved_href = urljoin(home_url, raw_href)
                parsed = urlsplit(resolved_href)
                if parsed.hostname and parsed.hostname.lower() != "www.vivo.com":
                    continue
                qs = parse_qs(parsed.query)
                id_vals = qs.get("id") or []
                model_id = (id_vals[0] if id_vals else "").strip()
                if not model_id or not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", model_id):
                    continue

                raw_label = _RE_WHITESPACE.sub(" ", anchor.get_text(" ", strip=True)).strip()
                if not raw_label:
                    raw_label = _RE_WHITESPACE.sub(
                        " ",
                        str(anchor.get("title") or anchor.get("data-model") or ""),
                    ).strip()
                if not raw_label:
                    img = anchor.find("img", alt=True)
                    if img is not None:
                        raw_label = _RE_WHITESPACE.sub(" ", str(img.get("alt") or "")).strip()
                if not raw_label:
                    continue
                if _RE_EXCLUDED_VIVO_ACCESSORY.search(raw_label):
                    continue

                # Strip leading redundant "vivo " so regional portals group consistently
                base_name = re.sub(r"^vivo\s+", "", raw_label, flags=re.IGNORECASE).strip()
                slug_seed = _RE_SLUG_NON_ALNUM.sub("-", base_name or raw_label).strip("-").lower()
                if not slug_seed:
                    continue
                code = canonical_code("vivo", slug_seed)
                if not code or code == "__global__":
                    continue

                if re.match(r"^iqoo\b", base_name, re.IGNORECASE):
                    display_label = "iQOO" + base_name[4:]
                else:
                    display_label = format_marketing_name(
                        {
                            "vendor": "vivo",
                            "upstream_id": code,
                            "codename": code,
                            "name": f"vivo {base_name}",
                        }
                    )

                if code not in state_by_code:
                    state_by_code[code] = {
                        "marketing_name": display_label or raw_label,
                        "region_model_ids": {region: model_id},
                        "targets": [{"region": region, "model_id": model_id}],
                    }
                else:
                    st = state_by_code[code]
                    st["region_model_ids"].setdefault(region, model_id)
                    pair = {"region": region, "model_id": model_id}
                    if pair not in st["targets"]:
                        st["targets"].append(pair)

        devices: list[AdapterDevice] = []
        for code in sorted(state_by_code):
            st = state_by_code[code]
            region_map = dict(st["region_model_ids"])
            devices.append(
                AdapterDevice(
                    codename=code,
                    brand_id="vivo",
                    marketing_name=st["marketing_name"],
                    model_numbers=(),
                    boot_partition_type="init_boot",
                    has_vbmeta=1,
                    vendor_meta={
                        "regions": dict(region_map),
                        "region_model_ids": dict(region_map),
                        "targets": list(st["targets"]),
                    },
                )
            )
        return devices

    def _extract_portal_targets(self, device: AdapterDevice) -> list[tuple[str, str]]:
        meta = device.vendor_meta or {}
        targets: list[tuple[str, str]] = []
        raw_targets = meta.get("targets")
        if isinstance(raw_targets, list):
            for entry in raw_targets:
                if isinstance(entry, dict) and entry.get("region") and entry.get("model_id"):
                    pair = (str(entry["region"]).strip().lower(), str(entry["model_id"]).strip())
                    if pair not in targets:
                        targets.append(pair)

        for map_key in ("region_model_ids", "regions"):
            mapping = meta.get(map_key)
            if isinstance(mapping, dict):
                for reg, mid in mapping.items():
                    if reg and mid:
                        pair = (str(reg).strip().lower(), str(mid).strip())
                        if pair not in targets:
                            targets.append(pair)
        return targets

    def fetch_releases(
        self,
        device: AdapterDevice,
        http: OfficialMetadataHTTP,
    ) -> list[AdapterRelease]:
        targets = self._extract_portal_targets(device)
        if not targets:
            return []

        releases: list[AdapterRelease] = []
        seen_artifacts: set[tuple[str, str]] = set()

        for region, model_id in targets:
            if not re.fullmatch(r"[a-z]{2,8}", region) or not re.fullmatch(
                r"[A-Za-z0-9_-]{1,32}", model_id
            ):
                continue
            portal_path = VIVO_PORTAL_LOCALE_PATHS.get(region, region)
            data_url = VIVO_DATA_URL_TEMPLATE.format(
                region=portal_path,
                model_id=quote(model_id, safe=""),
            )
            try:
                resp = http.get(data_url, headers=_VIVO_BROWSER_HEADERS)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in (400, 403, 404, 410, 418, 451):
                    continue
                raise

            soup = BeautifulSoup(resp.text, "html.parser")

            zip_elements: dict[str, Any] = {}
            for el in soup.find_all(True):
                for attr in ("href", "data-href", "data-url", "data-download"):
                    raw_val = el.get(attr)
                    if not isinstance(raw_val, str) or not raw_val.strip():
                        continue
                    norm_url = _normalize_vivo_zip_url(raw_val, data_url)
                    if norm_url and norm_url not in zip_elements:
                        zip_elements[norm_url] = el

            zip_urls = list(zip_elements.keys())
            # Graceful skip on newer PDF-only / OTA-notice-only vivo support pages
            if not zip_urls:
                continue

            def _find_exclusive_zip_container(target_url: str, start_el: Any) -> Any:
                best = start_el
                cur = getattr(start_el, "parent", None)
                while cur is not None and getattr(cur, "name", None) not in (
                    None,
                    "[document]",
                    "html",
                    "body",
                ):
                    other_found = False
                    for cand_el in cur.find_all(True):
                        for attr in ("href", "data-href", "data-url", "data-download"):
                            rv = cand_el.get(attr)
                            if not isinstance(rv, str) or not rv.strip():
                                continue
                            nu = _normalize_vivo_zip_url(rv, data_url)
                            if nu and nu != target_url and nu in zip_elements:
                                other_found = True
                                break
                        if other_found:
                            break
                    if other_found:
                        break
                    best = cur
                    cur = getattr(cur, "parent", None)
                return best

            page_text = _RE_WHITESPACE.sub(" ", soup.get_text(" ", strip=True)).strip()
            funtouch_el = soup.select_one("#funtouchName")
            dom_version = (
                _strip_vivo_ui_badges(_RE_WHITESPACE.sub(" ", funtouch_el.get_text(" ", strip=True)))
                if funtouch_el is not None
                else ""
            )

            page_pub_date = _extract_release_date(page_text)
            page_size_bytes, page_size_str = _extract_package_size(page_text)
            m_page_hash = _RE_LABELED_HASH.search(page_text)
            page_checksum = m_page_hash.group(1).lower() if m_page_hash else None

            m_origin = _RE_ORIGINOS_TEXT.search(page_text)
            m_funtouch = _RE_FUNTOUCH_TEXT.search(page_text)
            if m_origin:
                os_name = "OriginOS"
                os_ver: str | None = m_origin.group(1)
            elif m_funtouch:
                os_name = "Funtouch OS"
                os_ver = m_funtouch.group(1)
            else:
                os_name = "Funtouch OS"
                os_ver = None

            m_page_android = _RE_ANDROID_TEXT.search(page_text)

            for download_url in zip_urls:
                filename = unquote(PurePosixPath(urlsplit(download_url).path).name)
                fn_version = _strip_vivo_ui_badges(_extract_version_from_filename(filename) or "")

                if len(zip_urls) == 1:
                    raw_version = dom_version or fn_version
                    if not raw_version:
                        m_text_pd = _RE_VIVO_BUILD.search(page_text)
                        if m_text_pd:
                            raw_version = _strip_vivo_ui_badges(m_text_pd.group(1))
                    pub_date = page_pub_date
                    size_bytes, size_str = page_size_bytes, page_size_str
                    checksum = page_checksum
                    m_android = m_page_android
                else:
                    container = _find_exclusive_zip_container(download_url, zip_elements[download_url])
                    container_text = _RE_WHITESPACE.sub(" ", container.get_text(" ", strip=True)).strip()
                    local_ft_el = (
                        container.select_one("#funtouchName")
                        if hasattr(container, "select_one")
                        else None
                    )
                    local_dom_ver = (
                        _strip_vivo_ui_badges(
                            _RE_WHITESPACE.sub(" ", local_ft_el.get_text(" ", strip=True))
                        )
                        if local_ft_el is not None
                        else ""
                    )
                    raw_version = fn_version or local_dom_ver
                    if not raw_version:
                        m_local_pd = _RE_VIVO_BUILD.search(container_text)
                        if m_local_pd:
                            raw_version = _strip_vivo_ui_badges(m_local_pd.group(1))
                    pub_date = _extract_release_date(container_text) or page_pub_date
                    size_bytes, size_str = _extract_package_size(container_text)
                    m_local_hash = _RE_LABELED_HASH.search(container_text)
                    checksum = m_local_hash.group(1).lower() if m_local_hash else None
                    m_android = _RE_ANDROID_TEXT.search(container_text) or m_page_android

                if not raw_version:
                    continue

                m_pd = _RE_PD_MODEL.search(raw_version) or _RE_PD_MODEL.search(filename)
                pd_model = m_pd.group(1).upper() if m_pd else device.codename

                dedup_key = (raw_version, download_url)
                if dedup_key in seen_artifacts:
                    continue
                seen_artifacts.add(dedup_key)

                if m_android:
                    android_ver = m_android.group(1)
                else:
                    m_ver_maj = _RE_VERSION_ANDROID_MAJOR.search(raw_version)
                    if m_ver_maj and 11 <= int(m_ver_maj.group(1)) <= 19:
                        android_ver = str(int(m_ver_maj.group(1)))
                    elif os_ver and os_ver.split(".", 1)[0].isdigit() and 11 <= int(os_ver.split(".", 1)[0]) <= 19:
                        android_ver = os_ver.split(".", 1)[0]
                    else:
                        android_ver = "unknown"

                effective_os_ver = os_ver
                if effective_os_ver is None:
                    m_ver_maj = _RE_VERSION_ANDROID_MAJOR.search(raw_version)
                    if m_ver_maj and 11 <= int(m_ver_maj.group(1)) <= 19:
                        effective_os_ver = str(int(m_ver_maj.group(1)))

                region_code = "Global" if region == "en" else region.upper()

                releases.append(
                    AdapterRelease(
                        device_codename=device.codename,
                        model_code=pd_model,
                        region_code=region_code,
                        version_str=canonical_version_str(raw_version),
                        android_version=android_ver,
                        package_type="recovery_zip",
                        original_official_url=download_url,
                        fast_cdn_url=download_url,
                        md5_or_sha256=checksum,
                        published_date=pub_date,
                        file_size_bytes=size_bytes,
                        file_size_str=size_str,
                        delivery_mode="direct_cdn",
                        verification_status="VERIFIED_IMMUTABLE",
                        display_version=raw_version,
                        raw_version=raw_version,
                        os_name=os_name,
                        os_version=effective_os_ver,
                        original_filename=filename,
                        vendor_fields={
                            "pd_model": pd_model,
                            "portal_region": region,
                            "model_id": model_id,
                        },
                        sources={"vivo_support": data_url},
                        availability="available",
                    )
                )

        return releases


__all__ = [
    "ALLOWED_VIVO_ZIP_DOMAINS",
    "DEFAULT_VIVO_REGIONS",
    "VIVO_DATA_URL_TEMPLATE",
    "VIVO_HOME_URL_TEMPLATE",
    "VIVO_PORTAL_LOCALE_PATHS",
    "VivoAdapter",
]
