"""Official Google Play `supported_devices.csv` parser and category filter.

Source: https://storage.googleapis.com/play_public/supported_devices.csv
Supports both UTF-16LE/BE (Google Play's default) and UTF-8 / UTF-8-BOM encodings.
"""

from __future__ import annotations

from collections.abc import Iterable
import csv
from dataclasses import dataclass
import io
import re

from .base import OfficialMetadataHTTP

GOOGLE_PLAY_DEVICES_URL = "https://storage.googleapis.com/play_public/supported_devices.csv"

_RE_VALID_CODENAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_RE_WHITESPACE = re.compile(r"\s+")

# Non-phone / non-tablet device category markers excluded unconditionally.
_RE_EXCLUDED_NON_MOBILE = re.compile(
    r"(?:"
    r"\bandroid\s+tv\b"
    r"|\bgoogle\s+tv\b"
    r"|\bchromebook\b"
    r"|\bchrome\s*os\b"
    r"|\bchromebox\b"
    r"|\bchromebase\b"
    r"|\bsmart\s+tv\b"
    r"|\bprojector\b"
    r"|\btv\s+box\b"
    r"|\bstreaming\s+box\b"
    r"|\bset[- ]top\s+box\b"
    r"|\bbox\b"
    r"|\bdongle\b"
    r")",
    re.IGNORECASE,
)

# Wearable markers excluded unless include_wearables=True.
_RE_EXCLUDED_WEARABLE = re.compile(
    r"(?:\bwatch\b|\bsmartwatch\b|\bwear\s*os\b)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PlayDeviceRecord:
    retail_branding: str
    marketing_name: str
    codename: str
    model: str


@dataclass(frozen=True)
class PlayCatalogDevice:
    codename: str
    retail_branding: str
    marketing_name: str
    models: tuple[str, ...]


def _normalize_brand(raw: str) -> str:
    return _RE_WHITESPACE.sub(" ", raw.strip()).lower()


def decode_supported_devices_bytes(raw_bytes: bytes) -> str:
    """Decode Google Play supported_devices.csv from UTF-16LE/BE or UTF-8 (with or without BOM)."""
    if not raw_bytes:
        return ""
    if raw_bytes.startswith(b"\xff\xfe"):
        return raw_bytes[2:].decode("utf-16-le", errors="replace")
    if raw_bytes.startswith(b"\xfe\xff"):
        return raw_bytes[2:].decode("utf-16-be", errors="replace")
    if raw_bytes.startswith(b"\xef\xbb\xbf"):
        return raw_bytes.decode("utf-8-sig", errors="replace")

    sample = raw_bytes[:128]
    if b"\x00" in sample:
        even_nulls = sum(1 for idx in range(0, len(sample), 2) if sample[idx] == 0)
        odd_nulls = sum(1 for idx in range(1, len(sample), 2) if sample[idx] == 0)
        if odd_nulls > even_nulls:
            return raw_bytes.decode("utf-16-le", errors="replace")
        if even_nulls > odd_nulls:
            return raw_bytes.decode("utf-16-be", errors="replace")

    return raw_bytes.decode("utf-8", errors="replace")


def _is_excluded_category(
    *,
    marketing_name: str,
    codename: str,
    model: str,
    include_wearables: bool,
) -> bool:
    code_lower = codename.lower()
    # ChromeOS ARC++ / ARCVM containers & Android TV board suffixes in Google Play CSV
    if code_lower.endswith(("_cheets", "_cheets2", "_atv")):
        return True

    combined = f"{marketing_name} | {model}"
    if _RE_EXCLUDED_NON_MOBILE.search(combined):
        return True
    if not include_wearables and _RE_EXCLUDED_WEARABLE.search(combined):
        return True
    return False


def parse_google_play_devices_csv(
    raw_bytes: bytes | str,
    *,
    brands: str | Iterable[str] | None = None,
    include_wearables: bool = False,
) -> list[PlayDeviceRecord]:
    """Parse Google's official supported_devices.csv and filter to phones/tablets."""
    text = (
        decode_supported_devices_bytes(raw_bytes)
        if isinstance(raw_bytes, (bytes, bytearray))
        else str(raw_bytes)
    )
    if not text.strip():
        return []

    allowed_brands: set[str] | None = None
    if brands is not None:
        if isinstance(brands, str):
            allowed_brands = {_normalize_brand(brands)}
        else:
            allowed_brands = {_normalize_brand(b) for b in brands if str(b).strip()}

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return []

    # Map normalized header names cleanly in case of stray BOM or whitespace
    field_map: dict[str, str] = {}
    for col in reader.fieldnames:
        if col is None:
            continue
        clean_col = col.lstrip("\ufeff").strip().lower()
        field_map[clean_col] = col

    brand_col = field_map.get("retail branding")
    marketing_col = field_map.get("marketing name")
    device_col = field_map.get("device")
    model_col = field_map.get("model")
    if not (brand_col and device_col and model_col):
        raise ValueError(f"Unexpected supported_devices.csv header: {reader.fieldnames!r}")

    seen_pairs: set[tuple[str, str]] = set()
    records: list[PlayDeviceRecord] = []

    for row in reader:
        raw_brand = (row.get(brand_col) or "").strip()
        if not raw_brand:
            continue
        norm_brand = _normalize_brand(raw_brand)
        if allowed_brands is not None and norm_brand not in allowed_brands:
            continue

        raw_codename = (row.get(device_col) or "").strip().lower()
        raw_model = _RE_WHITESPACE.sub(" ", (row.get(model_col) or "").strip())
        raw_marketing = (
            _RE_WHITESPACE.sub(" ", (row.get(marketing_col) or "").strip())
            if marketing_col
            else ""
        )

        if not raw_codename or not raw_model:
            continue
        if raw_codename == "__global__" or not _RE_VALID_CODENAME.fullmatch(raw_codename):
            continue

        marketing_display = raw_marketing or raw_model
        if _is_excluded_category(
            marketing_name=marketing_display,
            codename=raw_codename,
            model=raw_model,
            include_wearables=include_wearables,
        ):
            continue

        pair_key = (raw_codename, raw_model)
        if pair_key in seen_pairs:
            continue
        seen_pairs.add(pair_key)

        records.append(
            PlayDeviceRecord(
                retail_branding=_RE_WHITESPACE.sub(" ", raw_brand),
                marketing_name=marketing_display,
                codename=raw_codename,
                model=raw_model,
            )
        )

    return records


def group_play_devices_by_codename(
    records: Iterable[PlayDeviceRecord],
) -> dict[str, PlayCatalogDevice]:
    """Group deduplicated PlayDeviceRecord rows by codename."""
    grouped_models: dict[str, list[str]] = {}
    grouped_meta: dict[str, tuple[str, str]] = {}

    for rec in records:
        if rec.codename not in grouped_meta:
            grouped_meta[rec.codename] = (rec.retail_branding, rec.marketing_name)
            grouped_models[rec.codename] = []
        else:
            existing_brand, existing_name = grouped_meta[rec.codename]
            if existing_name == rec.model and rec.marketing_name != rec.model:
                grouped_meta[rec.codename] = (existing_brand, rec.marketing_name)
        if rec.model not in grouped_models[rec.codename]:
            grouped_models[rec.codename].append(rec.model)

    return {
        codename: PlayCatalogDevice(
            codename=codename,
            retail_branding=grouped_meta[codename][0],
            marketing_name=grouped_meta[codename][1],
            models=tuple(grouped_models[codename]),
        )
        for codename in sorted(grouped_meta)
    }


def fetch_google_play_devices(
    http: OfficialMetadataHTTP,
    *,
    brands: str | Iterable[str] | None = None,
    include_wearables: bool = False,
    url: str = GOOGLE_PLAY_DEVICES_URL,
) -> list[PlayDeviceRecord]:
    """Fetch and parse Google's official supported_devices.csv using OfficialMetadataHTTP."""
    resp = http.get(url)
    return parse_google_play_devices_csv(
        resp.content,
        brands=brands,
        include_wearables=include_wearables,
    )


__all__ = [
    "GOOGLE_PLAY_DEVICES_URL",
    "PlayCatalogDevice",
    "PlayDeviceRecord",
    "decode_supported_devices_bytes",
    "fetch_google_play_devices",
    "group_play_devices_by_codename",
    "parse_google_play_devices_csv",
]
