"""Station #1: 100% Official Google Pixel & Nexus Stock Firmware Adapter.

Scrapes exclusively:
- https://developers.google.com/android/ota?hl=en
- https://developers.google.com/android/images?hl=en
Optionally enriches model numbers from Google's official Play supported_devices.csv.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import datetime
from html.parser import HTMLParser
from pathlib import PurePosixPath
import re
from typing import Literal
from urllib.parse import urlsplit

from fmr_ingest_stock.cdn_rewriter import validate_official_download_url

from .base import AdapterDevice, AdapterRelease, OfficialMetadataHTTP, VendorAdapter
from .google_play_devices import (
    GOOGLE_PLAY_DEVICES_URL,
    fetch_google_play_devices,
    group_play_devices_by_codename,
)

GOOGLE_OTA_URL = "https://developers.google.com/android/ota?hl=en"
GOOGLE_IMAGES_URL = "https://developers.google.com/android/images?hl=en"
GOOGLE_OTA_COOKIES = {"devsite_wall_acks": "nexus-ota-tos"}
GOOGLE_IMAGES_COOKIES = {"devsite_wall_acks": "nexus-image-tos"}

_RE_HEADING_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{1,31}$")
_EXCLUDED_HEADING_IDS: frozenset[str] = frozenset(
    {
        "acknowledgements",
        "building",
        "devices",
        "downloads",
        "factory-images",
        "flashing",
        "images",
        "instructions",
        "latest",
        "legal",
        "locking",
        "nexus",
        "notes",
        "ota",
        "ota-images",
        "overview",
        "pixel",
        "reverting",
        "security",
        "terms",
        "top",
        "tos",
        "unlocking",
        "updating",
        "warning",
        "warnings",
    }
)

# Legacy Google Nexus software product names that target a distinct hardware board codename.
KNOWN_NEXUS_PRODUCT_TO_BOARD: dict[str, str] = {
    "yakju": "maguro",
    "takju": "maguro",
    "mysid": "toro",
    "mysidspr": "toroplus",
    "soju": "crespo",
    "sojua": "crespo",
    "sojuk": "crespo",
    "sojus": "crespo4g",
    "nakasi": "grouper",
    "nakasig": "tilapia",
    "razor": "flo",
    "razorg": "deb",
    "occam": "mako",
    "mantaray": "manta",
    "volantis": "flounder",
    "volantisg": "flounder_lte",
    "tungsten": "steelhead",
}

_MONTH_MAP: dict[str, int] = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}

_NON_BUILD_TOKENS_UPPER: frozenset[str] = frozenset(
    {
        *(m.upper() for m in _MONTH_MAP),
        "O2",
        "EE",
        "UK",
        "EU",
        "US",
        "USA",
        "JP",
        "AU",
        "TW",
        "IN",
        "CA",
        "SG",
        "DE",
        "FR",
        "IT",
        "ES",
        "EMEA",
        "ROW",
        "APAC",
        "LATAM",
        "NA",
        "FI",
        "GOOGLE FI",
        "PROJECT FI",
        "ATT",
        "AT&T",
        "TMO",
        "T-MOBILE",
        "VERIZON",
        "VERIZON MVNOS",
        "VZW",
        "SPRINT",
        "TELSTRA",
        "OPTUS",
        "VODAFONE",
        "ROGERS",
        "BELL",
        "TELUS",
        "SOFTBANK",
        "KDDI",
        "DOCOMO",
        "NTT DOCOMO",
        "4G",
        "5G",
        "LTE",
        "GSM",
        "CDMA",
        "MMWAVE",
        "SUB-6",
        "UNLOCKED",
    }
)

_RE_SHA256 = re.compile(r"\b([0-9a-fA-F]{64})\b")
_RE_WHITESPACE = re.compile(r"\s+")
_RE_WALLET_NOISE = re.compile(r"\s*\(with\s+google\s+wallet\)", re.IGNORECASE)
_RE_QUOTED_HEADING = re.compile(
    r'^["\u201c\u201d]?([a-z0-9][a-z0-9_-]{1,31})["\u201c\u201d]?\s+for\s+(.+)$',
    re.IGNORECASE,
)
_RE_EMBEDDED_BOARD = re.compile(r'["\u201c\u201d]([a-z0-9][a-z0-9_-]{1,31})["\u201c\u201d]')
_RE_MONTH_YEAR = re.compile(
    r"\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
    r"\.?\s+(\d{4})\b",
    re.IGNORECASE,
)
_RE_MODERN_AOSP_BUILD = re.compile(
    r"^[A-Z0-9]{3,6}\.\d{6}\.\d{3}(?:\.[A-Z0-9]+)*$",
    re.IGNORECASE,
)
_RE_LEGACY_AOSP_BUILD = re.compile(r"^[A-Z]{2,4}\d{2,3}[A-Z0-9]{1,3}$")
_RE_SECURITY_PATCH_BUILD = re.compile(r"^[A-Z]{2}\d[A-Z]\.(\d{2})(\d{2})(\d{2})\.\d+")
_RE_EXACT_VERSION_PREFIX = re.compile(r"^(?:Android\s+)?(\d+(?:\.\d+)*L?)$", re.IGNORECASE)
_RE_MAJOR_IN_TEXT = re.compile(r"(?:^|\bAndroid\s+)(\d{1,2}L?)\b", re.IGNORECASE)


@dataclass
class _RawCell:
    text_parts: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return _RE_WHITESPACE.sub(" ", "".join(self.text_parts)).strip()


@dataclass
class _RawSectionTable:
    heading_tag: str
    heading_id: str
    heading_text: str
    parent_h2_id: str | None
    parent_h2_text: str | None
    rows: list[list[_RawCell]]


class _DevSiteTableParser(HTMLParser):
    """Resilient stdlib HTML parser for Google DevSite firmware tables (h2/h3/h4 + table)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.sections: list[_RawSectionTable] = []

        self._in_heading_tag: str | None = None
        self._heading_id: str | None = None
        self._heading_data_text: str | None = None
        self._heading_chars: list[str] = []

        self._current_h2: tuple[str, str] | None = None
        self._active_heading: tuple[str, str, str] | None = None  # (tag, id, text)

        self._in_table = False
        self._table_rows: list[list[_RawCell]] = []
        self._current_row: list[_RawCell] | None = None
        self._current_cell: _RawCell | None = None
        self._in_header_cell = False

    def _close_cell(self) -> None:
        if self._current_cell is not None and self._current_row is not None:
            if not self._in_header_cell:
                self._current_row.append(self._current_cell)
        self._current_cell = None
        self._in_header_cell = False

    def _close_row(self) -> None:
        self._close_cell()
        if self._current_row:
            self._table_rows.append(self._current_row)
        self._current_row = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag_l = tag.lower()
        attr_dict = {k.lower(): (v or "") for k, v in attrs}

        if tag_l in ("h2", "h3", "h4"):
            raw_id = attr_dict.get("id", "").strip()
            self._in_heading_tag = tag_l
            self._heading_id = raw_id
            self._heading_data_text = attr_dict.get("data-text", "").strip() or None
            self._heading_chars = []
            return

        if tag_l == "table":
            if self._active_heading is not None:
                self._in_table = True
                self._table_rows = []
            return

        if not self._in_table:
            return

        if tag_l == "tr":
            self._close_row()
            self._current_row = []
        elif tag_l in ("td", "th"):
            self._close_cell()
            if self._current_row is None:
                self._current_row = []
            self._current_cell = _RawCell()
            self._in_header_cell = tag_l == "th"
        elif tag_l == "a" and self._current_cell is not None:
            href = attr_dict.get("href", "").strip()
            if href:
                self._current_cell.links.append(href)

    def handle_data(self, data: str) -> None:
        if self._in_heading_tag is not None:
            self._heading_chars.append(data)
        if self._in_table and self._current_cell is not None:
            self._current_cell.text_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag_l = tag.lower()
        if tag_l == self._in_heading_tag:
            raw_id = (self._heading_id or "").strip().lower()
            text = _RE_WHITESPACE.sub(" ", "".join(self._heading_chars)).strip()
            if not text and self._heading_data_text:
                text = _RE_WHITESPACE.sub(" ", self._heading_data_text).strip()

            if _RE_HEADING_ID.fullmatch(raw_id) and raw_id not in _EXCLUDED_HEADING_IDS:
                if tag_l == "h2":
                    self._current_h2 = (raw_id, text)
                self._active_heading = (tag_l, raw_id, text)
            elif tag_l == "h2":
                self._current_h2 = None
                self._active_heading = None

            self._in_heading_tag = None
            self._heading_id = None
            self._heading_data_text = None
            self._heading_chars = []
            return

        if not self._in_table:
            return

        if tag_l in ("td", "th"):
            self._close_cell()
        elif tag_l == "tr":
            self._close_row()
        elif tag_l == "table":
            self._close_row()
            if self._active_heading is not None and self._table_rows:
                h_tag, h_id, h_text = self._active_heading
                p_id = self._current_h2[0] if self._current_h2 else None
                p_text = self._current_h2[1] if self._current_h2 else None
                self.sections.append(
                    _RawSectionTable(
                        heading_tag=h_tag,
                        heading_id=h_id,
                        heading_text=h_text,
                        parent_h2_id=p_id,
                        parent_h2_text=p_text,
                        rows=self._table_rows,
                    )
                )
            self._in_table = False
            self._table_rows = []


def _clean_marketing_name(raw_desc: str, fallback_codename: str) -> str:
    cleaned = _RE_WALLET_NOISE.sub("", raw_desc)
    cleaned = _RE_WHITESPACE.sub(" ", cleaned).strip(" -–—")
    if not cleaned:
        return f"Google {fallback_codename}"
    if cleaned.lower().startswith("google "):
        return "Google " + cleaned[7:].strip()
    return f"Google {cleaned}"


def _resolve_heading_identity(
    heading_tag: str,
    heading_id: str,
    heading_text: str,
    parent_h2_id: str | None = None,
    parent_h2_text: str | None = None,
) -> tuple[str, str, str]:
    """Return (device_codename, model_code, marketing_name) from a DevSite heading."""
    norm_id = heading_id.strip().lower()
    text = heading_text.strip()

    first_code = norm_id
    desc = ""
    m = _RE_QUOTED_HEADING.match(text)
    if m:
        first_code = m.group(1).lower()
        desc = m.group(2).strip()
    elif text:
        # Strip leading quoted id if present without "for"
        stripped = re.sub(
            rf'^["\u201c\u201d]?{re.escape(norm_id)}["\u201c\u201d]?\s*[-:]?\s*',
            "",
            text,
            flags=re.IGNORECASE,
        ).strip()
        desc = stripped

    board_code: str | None = None
    embedded = _RE_EMBEDDED_BOARD.search(desc)
    if embedded:
        candidate = embedded.group(1).lower()
        if _RE_HEADING_ID.fullmatch(candidate) and candidate not in _EXCLUDED_HEADING_IDS:
            board_code = candidate
            desc = (desc[: embedded.start()] + " " + desc[embedded.end() :]).strip()

    if board_code is None and first_code in KNOWN_NEXUS_PRODUCT_TO_BOARD:
        board_code = KNOWN_NEXUS_PRODUCT_TO_BOARD[first_code]
    elif (
        board_code is None
        and not desc
        and heading_tag in ("h3", "h4")
        and parent_h2_id
        and parent_h2_id != norm_id
        and _RE_HEADING_ID.fullmatch(parent_h2_id)
        and parent_h2_id not in _EXCLUDED_HEADING_IDS
    ):
        board_code = parent_h2_id
        if parent_h2_text:
            pm = _RE_QUOTED_HEADING.match(parent_h2_text.strip())
            if pm:
                desc = pm.group(2).strip()

    raw_codename = board_code or norm_id
    model_code = first_code if board_code else raw_codename
    marketing_name = _clean_marketing_name(desc, raw_codename)
    device_codename = raw_codename if raw_codename.startswith("google-") else f"google-{raw_codename}"
    return device_codename, model_code, marketing_name


def derive_aosp_security_patch(build_id: str) -> str | None:
    """Derive YYYY-MM-DD security patch date from standard AOSP release build IDs."""
    m = _RE_SECURITY_PATCH_BUILD.match(build_id.strip().upper())
    if not m:
        return None
    yy, mm, dd = int(m.group(1)), int(m.group(2)), int(m.group(3))
    year = 2000 + yy
    if not (2015 <= year <= 2099):
        return None
    try:
        dt = datetime.date(year, mm, dd)
    except ValueError:
        return None
    return dt.isoformat()


def _parse_android_version_and_major(prefix_text: str, full_caption: str) -> tuple[str, int | None]:
    prefix = prefix_text.strip()
    if re.search(r"\b12L\b", prefix, re.IGNORECASE) or re.search(r"\b12L\b", full_caption, re.IGNORECASE):
        return "12L", 12

    m_exact = _RE_EXACT_VERSION_PREFIX.match(prefix)
    if m_exact:
        ver = m_exact.group(1)
        if ver.upper().endswith("L"):
            ver = ver[:-1] + "L"
            return ver, int(ver[:-1])
        major = int(ver.split(".", 1)[0])
        return ver, major

    m_major = _RE_MAJOR_IN_TEXT.search(prefix)
    if m_major:
        tok = m_major.group(1)
        if tok.upper().endswith("L"):
            return tok[:-1] + "L", int(tok[:-1])
        return tok, int(tok)

    lower_cap = f"{prefix} {full_caption}".lower()
    if "developer preview" in lower_cap or re.search(r"\bdp\s*\d*\b", lower_cap) or "preview" in lower_cap:
        return "Preview", None
    if "beta" in lower_cap:
        return "Beta", None

    # Last-resort numeric prefix before space
    m_num = re.match(r"^(\d+(?:\.\d+)*)", prefix)
    if m_num:
        ver = m_num.group(1)
        return ver, int(ver.split(".", 1)[0])

    return "unknown", None


def _extract_filename_build_id(
    filename: str,
    codename: str,
    model_code: str,
) -> str | None:
    prefixes = list(dict.fromkeys([model_code.lower(), codename.lower()]))
    prefix_alt = "|".join(re.escape(p) for p in prefixes if p)
    pattern = re.compile(
        rf"^(?:{prefix_alt})-(?:ota-)?([a-z0-9._-]+?)-(?:factory-)?[0-9a-f]{{8,64}}\.(?:zip|tgz)$",
        re.IGNORECASE,
    )
    m = pattern.match(filename)
    if m:
        return m.group(1)
    # Generic fallback if Google used a slightly different device alias prefix in filename
    generic = re.match(
        r"^[a-z0-9][a-z0-9_-]{1,31}-(?:ota-)?([a-z0-9._-]+?)-(?:factory-)?[0-9a-f]{8,64}\.(?:zip|tgz)$",
        filename,
        re.IGNORECASE,
    )
    if generic:
        return generic.group(1)
    return None


def _is_valid_caption_build_token(token: str) -> bool:
    cleaned = token.strip()
    if not cleaned or len(cleaned) < 4:
        return False
    upper = cleaned.upper()
    if upper in _NON_BUILD_TOKENS_UPPER:
        return False
    if _RE_MONTH_YEAR.search(cleaned):
        return False
    if upper.isdigit():
        return False
    if _RE_MODERN_AOSP_BUILD.fullmatch(upper):
        return True
    if _RE_LEGACY_AOSP_BUILD.fullmatch(upper):
        return True
    return False


def parse_google_caption(
    caption: str,
    *,
    filename: str,
    codename: str,
    model_code: str,
) -> tuple[str, int | None, str, str | None, str]:
    """Parse a DevSite version caption + filename.

    Returns:
        (android_version, android_major, build_id, published_date, region_code)
    """
    norm_caption = _RE_WHITESPACE.sub(" ", caption).strip()
    paren_start = norm_caption.find("(")
    paren_end = norm_caption.rfind(")")
    if 0 <= paren_start < paren_end:
        prefix_part = norm_caption[:paren_start].strip()
        inside_paren = norm_caption[paren_start + 1 : paren_end].strip()
    else:
        prefix_part = norm_caption
        inside_paren = ""

    android_version, android_major = _parse_android_version_and_major(prefix_part, norm_caption)
    raw_tokens = [tok.strip() for tok in inside_paren.split(",") if tok.strip()] if inside_paren else []

    fn_build_raw = _extract_filename_build_id(filename, codename=codename, model_code=model_code)
    build_id: str | None = None
    build_token_idx: int | None = None

    if fn_build_raw:
        fn_lower = fn_build_raw.lower()
        for idx, tok in enumerate(raw_tokens):
            if tok.lower() == fn_lower:
                build_id = tok
                build_token_idx = idx
                break
        if build_id is None:
            build_id = fn_build_raw.upper()
    else:
        for idx, tok in enumerate(raw_tokens):
            if _is_valid_caption_build_token(tok):
                build_id = tok
                build_token_idx = idx
                break

    if not build_id:
        raise ValueError(f"Unable to extract valid build ID from caption={caption!r}, filename={filename!r}")

    published_date: str | None = None
    channels: list[str] = []

    for idx, tok in enumerate(raw_tokens):
        if idx == build_token_idx or tok.lower() == build_id.lower():
            continue
        m_date = _RE_MONTH_YEAR.fullmatch(tok)
        if m_date and published_date is None:
            month_num = _MONTH_MAP[m_date.group(1).lower()]
            year_num = int(m_date.group(2))
            published_date = f"{year_num:04d}-{month_num:02d}-01"
            continue
        m_embedded_date = _RE_MONTH_YEAR.search(tok)
        if m_embedded_date and published_date is None:
            month_num = _MONTH_MAP[m_embedded_date.group(1).lower()]
            year_num = int(m_embedded_date.group(2))
            published_date = f"{year_num:04d}-{month_num:02d}-01"
            remainder = (tok[: m_embedded_date.start()] + tok[m_embedded_date.end() :]).strip(" -–—,")
            if remainder and remainder not in channels:
                channels.append(remainder)
            continue
        if tok not in channels:
            channels.append(tok)

    if channels:
        region_code = " · ".join(channels)[:64].rstrip(" ·")
        if not region_code:
            region_code = "Global"
    else:
        region_code = "Global"

    return android_version, android_major, build_id, published_date, region_code


@dataclass
class _ParsedGoogleEntry:
    codename: str
    model_code: str
    marketing_name: str
    android_major: int | None
    release: AdapterRelease


def _normalize_download_href(href: str) -> str | None:
    cleaned = href.strip()
    if not cleaned:
        return None
    if cleaned.startswith("//"):
        cleaned = "https:" + cleaned
    if not cleaned.startswith("https://"):
        return None
    parsed = urlsplit(cleaned)
    path_lower = parsed.path.lower()
    if not (path_lower.endswith(".zip") or path_lower.endswith(".tgz")):
        return None
    try:
        return validate_official_download_url("google", cleaned, resolve_dns=False)
    except ValueError:
        return None


def _parse_devsite_page(
    html_text: str,
    *,
    page_kind: Literal["ota", "images"],
    source_url: str,
) -> list[_ParsedGoogleEntry]:
    parser = _DevSiteTableParser()
    parser.feed(html_text)
    parser.close()

    entries: list[_ParsedGoogleEntry] = []
    for section in parser.sections:
        codename, model_code, marketing_name = _resolve_heading_identity(
            section.heading_tag,
            section.heading_id,
            section.heading_text,
            parent_h2_id=section.parent_h2_id,
            parent_h2_text=section.parent_h2_text,
        )

        for row in section.rows:
            if len(row) < 2:
                continue
            caption = row[0].text
            if not caption:
                continue

            download_url: str | None = None
            for cell in row[1:]:
                for link in cell.links:
                    normalized = _normalize_download_href(link)
                    if normalized is not None:
                        download_url = normalized
                        break
                if download_url is not None:
                    break
            if download_url is None:
                continue

            sha256_hex: str | None = None
            for cell in reversed(row[1:]):
                m_sha = _RE_SHA256.search(cell.text)
                if m_sha:
                    sha256_hex = m_sha.group(1).lower()
                    break
            if sha256_hex is None:
                continue

            filename = PurePosixPath(urlsplit(download_url).path).name
            if not filename:
                continue

            fn_lower = filename.lower()
            if fn_lower.endswith(".tgz"):
                package_type = "fastboot_tgz"
            elif page_kind == "ota":
                package_type = "recovery_zip"
            else:
                package_type = "factory_zip"

            try:
                android_version, android_major, build_id, published_date, region_code = (
                    parse_google_caption(
                        caption,
                        filename=filename,
                        codename=codename,
                        model_code=model_code,
                    )
                )
            except ValueError:
                continue

            security_patch = derive_aosp_security_patch(build_id)
            source_key = "google_ota" if page_kind == "ota" else "google_factory"

            release = AdapterRelease(
                device_codename=codename,
                model_code=model_code,
                region_code=region_code,
                version_str=build_id,
                android_version=android_version,
                package_type=package_type,
                original_official_url=download_url,
                fast_cdn_url=download_url,
                md5_or_sha256=sha256_hex,
                published_date=published_date,
                delivery_mode="direct_cdn",
                verification_status="VERIFIED_IMMUTABLE",
                display_version=build_id,
                raw_version=caption,
                os_name="Android",
                os_version=None if android_version == "unknown" else android_version,
                security_patch=security_patch,
                original_filename=filename,
                vendor_fields={
                    "build_id": build_id,
                    "channel": region_code,
                    "page_kind": page_kind,
                    "variant_code": model_code,
                },
                sources={source_key: source_url},
                availability="available",
            )
            entries.append(
                _ParsedGoogleEntry(
                    codename=codename,
                    model_code=model_code,
                    marketing_name=marketing_name,
                    android_major=android_major,
                    release=release,
                )
            )

    return entries


class GoogleAdapter(VendorAdapter):
    """Official Google Pixel & Nexus stock firmware adapter (Station #1)."""

    vendor_id = "google"

    def __init__(
        self,
        *,
        ota_url: str = GOOGLE_OTA_URL,
        images_url: str = GOOGLE_IMAGES_URL,
        enrich_from_play_csv: bool = False,
        play_csv_url: str = GOOGLE_PLAY_DEVICES_URL,
    ) -> None:
        self.ota_url = ota_url
        self.images_url = images_url
        self.enrich_from_play_csv = enrich_from_play_csv
        self.play_csv_url = play_csv_url

        self._loaded_http_id: int | None = None
        self._devices_by_codename: dict[str, AdapterDevice] = {}
        self._releases_by_codename: dict[str, list[AdapterRelease]] = {}

    def _load_atomic_catalog(self, http: OfficialMetadataHTTP) -> None:
        ota_resp = http.get(self.ota_url, cookies=GOOGLE_OTA_COOKIES)
        ota_entries = _parse_devsite_page(
            ota_resp.text,
            page_kind="ota",
            source_url=self.ota_url,
        )
        if not ota_entries:
            http.invalidate(self.ota_url, cookies=GOOGLE_OTA_COOKIES)
            raise RuntimeError(
                f"Google OTA page ({self.ota_url}) yielded 0 valid releases (possible TOS wall shift); aborting atomically"
            )

        images_resp = http.get(self.images_url, cookies=GOOGLE_IMAGES_COOKIES)
        images_entries = _parse_devsite_page(
            images_resp.text,
            page_kind="images",
            source_url=self.images_url,
        )
        if not images_entries:
            http.invalidate(self.images_url, cookies=GOOGLE_IMAGES_COOKIES)
            raise RuntimeError(
                f"Google Factory Images page ({self.images_url}) yielded 0 valid releases (possible TOS wall shift); aborting atomically"
            )

        play_by_codename = {}
        if self.enrich_from_play_csv:
            play_records = fetch_google_play_devices(
                http,
                brands="google",
                include_wearables=False,
                url=self.play_csv_url,
            )
            play_by_codename = group_play_devices_by_codename(play_records)

        names_by_codename: dict[str, list[str]] = {}
        models_by_codename: dict[str, list[str]] = {}
        majors_by_codename: dict[str, list[int]] = {}
        releases_by_codename: dict[str, list[AdapterRelease]] = {}

        for entry in (*ota_entries, *images_entries):
            code = entry.codename
            names_by_codename.setdefault(code, [])
            models_by_codename.setdefault(code, [])
            majors_by_codename.setdefault(code, [])
            releases_by_codename.setdefault(code, [])

            if entry.marketing_name not in names_by_codename[code]:
                names_by_codename[code].append(entry.marketing_name)
            if entry.model_code and entry.model_code != code:
                if entry.model_code not in models_by_codename[code]:
                    models_by_codename[code].append(entry.model_code)
            if entry.android_major is not None:
                majors_by_codename[code].append(entry.android_major)
            releases_by_codename[code].append(entry.release)

        devices_out: dict[str, AdapterDevice] = {}
        for code in sorted(releases_by_codename):
            candidate_names = names_by_codename.get(code, [])
            fallback_name = f"Google {code}"
            chosen_name = fallback_name
            for cand in candidate_names:
                if cand.lower() != fallback_name.lower():
                    chosen_name = cand
                    break
            else:
                if candidate_names:
                    chosen_name = candidate_names[0]

            merged_models = list(models_by_codename.get(code, []))
            if code in play_by_codename:
                for play_model in play_by_codename[code].models:
                    if play_model not in merged_models:
                        merged_models.append(play_model)

            observed_majors = majors_by_codename.get(code, [])
            if observed_majors:
                earliest_major = min(observed_majors)
                boot_partition_type = "init_boot" if earliest_major >= 13 else "boot"
            else:
                boot_partition_type = "init_boot"

            devices_out[code] = AdapterDevice(
                codename=code,
                brand_id="google",
                marketing_name=chosen_name,
                model_numbers=tuple(merged_models),
                boot_partition_type=boot_partition_type,
                has_vbmeta=1,
                vendor_meta={
                    "earliest_android_major": min(observed_majors) if observed_majors else None,
                },
            )

        self._loaded_http_id = id(http)
        self._devices_by_codename = devices_out
        self._releases_by_codename = releases_by_codename

    def discover_devices(self, http: OfficialMetadataHTTP) -> list[AdapterDevice]:
        self._load_atomic_catalog(http)
        return [self._devices_by_codename[code] for code in sorted(self._devices_by_codename)]

    def fetch_releases(
        self,
        device: AdapterDevice,
        http: OfficialMetadataHTTP,
    ) -> list[AdapterRelease]:
        if self._loaded_http_id != id(http) or not self._releases_by_codename:
            self._load_atomic_catalog(http)
        return list(self._releases_by_codename.get(device.codename, ()))


__all__ = [
    "GOOGLE_IMAGES_COOKIES",
    "GOOGLE_IMAGES_URL",
    "GOOGLE_OTA_COOKIES",
    "GOOGLE_OTA_URL",
    "GoogleAdapter",
    "KNOWN_NEXUS_PRODUCT_TO_BOARD",
    "derive_aosp_security_patch",
    "parse_google_caption",
]
