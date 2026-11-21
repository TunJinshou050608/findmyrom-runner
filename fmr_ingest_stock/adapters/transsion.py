"""Official Transsion (TECNO / Infinix / itel) Stock Firmware Adapter.

Uses Google Play `supported_devices.csv` for official phone/tablet discovery and
pure-Python Android GOTA checkin (`https://android.googleapis.com/checkin`) via
`OfficialMetadataHTTP` to discover official Full-OTA ZIP packages (`Tcard_*`).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
import csv
import datetime
import io
from pathlib import PurePosixPath
import re
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

from fmr_core.publish_spec import canonical_version_str
from fmr_ingest_stock.cdn_rewriter import validate_official_download_url
from fmr_ingest_stock.device_identity import marketing_name as format_marketing_name
from fmr_ingest_stock.oem_sources import canonical_code
from fmr_ingest_stock.pipeline.verifier import format_file_size
from fmr_ingest_stock.release_metadata import iso_date
from fmr_ingest_stock.transsion_ota import (
    BRAND_TO_OS,
    GOOGLE_CHECKIN_URL,
    GoogleCheckinClient,
    parse_transsion_version,
)

from .base import AdapterDevice, AdapterRelease, OfficialMetadataHTTP, VendorAdapter
from .google_play_devices import (
    GOOGLE_PLAY_DEVICES_URL,
    _is_excluded_category,
    decode_supported_devices_bytes,
)

SUPPORTED_TRANSSION_VENDORS: tuple[str, ...] = ("tecno", "infinix", "itel")

TRANSSION_PLAY_BRANDS: dict[str, tuple[str, ...]] = {
    "tecno": ("tecno", "tecno mobile"),
    "infinix": ("infinix",),
    "itel": ("itel",),
}

TRANSSION_FINGERPRINT_BRANDS: dict[str, str] = {
    "tecno": "TECNO",
    "infinix": "Infinix",
    "itel": "Itel",
}

DEFAULT_ANDROID_BUILD_SEEDS: tuple[tuple[int, str], ...] = (
    (16, "BP2A.250605.031.A2"),
    (15, "AP3A.240905.015.A2"),
    (14, "UP1A.231005.007"),
    (13, "TP1A.220624.014"),
    (12, "SP1A.210812.016"),
)

DEFAULT_TRANSSION_REGIONS: tuple[str, ...] = (
    "OP",
    "GL",
    "EU",
    "IN",
    "RU",
    "ID",
    "TR",
    "MEA",
)

DEFAULT_MAX_PROBES_PER_DEVICE = 40
DEFAULT_REQUEST_DELAY_SECONDS = 0.05
DEFAULT_PROBE_WORKERS = 6
MIN_FULL_PACKAGE_BYTES = 50 * 1024 * 1024  # 50 MiB

_SDK_TO_ANDROID: dict[int, str] = {
    36: "16",
    35: "15",
    34: "14",
    33: "13",
    32: "12",
    31: "12",
    30: "11",
    29: "10",
}

_RE_BRAND_PREFIX = re.compile(r"^(?:tecno(?:[-_ ]*mobile)?|infinix|itel)[-_ ]+", re.IGNORECASE)
_OTHER_TRANSSION_PREFIX_RE: dict[str, re.Pattern[str]] = {
    "tecno": re.compile(r"^(?:infinix|itel)[-_ ]+", re.IGNORECASE),
    "infinix": re.compile(r"^(?:tecno(?:[-_ ]*mobile)?|itel)[-_ ]+", re.IGNORECASE),
    "itel": re.compile(r"^(?:tecno(?:[-_ ]*mobile)?|infinix)[-_ ]+", re.IGNORECASE),
}
_RE_TECNO_MOBILE_NAME = re.compile(r"^tecno[-_ ]+mobile\b", re.IGNORECASE)
_RE_EXCLUDED_TRANSSION_ACCESSORY = re.compile(
    r"(?:\b(?:watch|band|buds|earbuds|tws|router|tv|box|dongle|projector|ai\s*pont)\b|androidtv)",
    re.IGNORECASE,
)
_RE_VALID_CSV_CODENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_RE_WHITESPACE = re.compile(r"\s+")

TRANSSION_DISPLAY_BRANDS: dict[str, str] = {
    "tecno": "TECNO",
    "infinix": "Infinix",
    "itel": "itel",
}
_RE_OTA_SHA1 = re.compile(r"/packages/ota-api/package/([0-9a-fA-F]{40})\.zip$")
_RE_SAFE_FILENAME = re.compile(r"^[A-Za-z0-9._()+-]{1,200}$")
_RE_TRANSSION_MODERN_NAMES = re.compile(
    r"\b(?:PHANTOM|CAMON\s*(?:3\d|4\d)|POVA\s*(?:6|7|Curve)|SPARK\s*(?:2\d|3\d|Go\s*1)|MEGAPAD|"
    r"GT\s*(?:2\d|3\d)|NOTE\s*(?:4\d|5\d)|ZERO\s*(?:3\d|4\d|Flip)|HOT\s*(?:4\d|5\d)|XPAD|"
    r"S24|S25|P55|P65|A70|A80|RS4|CITY|VistaTab)\b",
    re.IGNORECASE,
)
_RE_TRANSSION_MID_NAMES = re.compile(
    r"\b(?:CAMON\s*(?:18|19|2\d)|POVA\s*(?:4|5)|SPARK\s*(?:9|1\d)|"
    r"GT\s*1\d|NOTE\s*(?:12|3\d)|ZERO\s*(?:2\d|ULTRA)|HOT\s*(?:2\d|3\d)|SMART\s*(?:7|8|9)|"
    r"S23|P40|A60)\b",
    re.IGNORECASE,
)
_RE_TRANSSION_LEGACY_CODES = re.compile(
    r"^(?:itel-(?:a1\d|a2\d|a3\d|a4\d|w\d+)|tecno-(?:y\d|wx\d|in\d|a571|a7)|infinix-(?:x4\d\d|x5\d\d|x60\d|x61\d|x62\d|up09))",
    re.IGNORECASE,
)


def _transsion_collect_priority(brand_id: str, marketing_name: str, codename: str) -> int:
    """Order recent Transsion releases ahead of legacy pre-GOTA models on cold scans."""
    if _RE_TRANSSION_MODERN_NAMES.search(marketing_name):
        return 10
    if _RE_TRANSSION_MID_NAMES.search(marketing_name):
        return 30
    if _RE_TRANSSION_LEGACY_CODES.search(codename):
        return 900
    if brand_id == "infinix" and re.match(r"^infinix-x(?:6[5-9]|11)", codename):
        return 100
    if brand_id == "itel" and re.match(r"^itel-(?:a6|p6|s6)", codename):
        return 100
    if brand_id == "tecno" and re.match(r"^tecno-(?:ad|ae|c[klm]|k[lj]|l[hi]|b[gf])", codename):
        return 100
    return 500


def _strip_brand_prefix(value: str) -> str:
    cleaned = _RE_BRAND_PREFIX.sub("", (value or "").strip()).strip()
    return cleaned or (value or "").strip()


def _normalize_sdk_android_version(parsed_ver: str | None, sdk_level: int | None) -> str:
    # Checkin `update_target_sdk_level` comes from OTA `post-sdk-level`; modern filename suffixes
    # like `_a15` can denote source baseline even on Android 16 (SDK 36) packages.
    if sdk_level is not None:
        if sdk_level in _SDK_TO_ANDROID:
            return _SDK_TO_ANDROID[sdk_level]
        if sdk_level > 36:
            return str(sdk_level - 20)
    if parsed_ver and parsed_ver.strip().lower() != "unknown":
        return parsed_ver.strip()
    return "unknown"


def _extract_os_version_from_description(description: str | None) -> str | None:
    if not description:
        return None
    m_to = re.search(
        r"(?:to|upgrade\s+to)\s+(?:HiOS|XOS|itel\s*OS|itelOS)\s*([0-9]+(?:\.[0-9]+)*)",
        description,
        re.IGNORECASE,
    )
    if m_to:
        return m_to.group(1)
    matches = re.findall(
        r"(?:HiOS|XOS|itel\s*OS|itelOS)\s*([0-9]+(?:\.[0-9]+)*)",
        description,
        re.IGNORECASE,
    )
    return matches[-1] if matches else None


def _build_safe_filename(title: str, download_url: str) -> str:
    base = re.sub(r"[^A-Za-z0-9._()+-]+", "_", (title or "").strip()).strip("._")
    if base:
        if not base.lower().endswith(".zip"):
            base = f"{base}.zip"
        if _RE_SAFE_FILENAME.fullmatch(base):
            return base
    return PurePosixPath(urlsplit(download_url).path).name


def _default_sleep_fn(http: OfficialMetadataHTTP) -> Callable[[float], None]:
    if isinstance(getattr(http._client, "_transport", None), httpx.MockTransport):
        return lambda _: None
    return time.sleep


class TranssionAdapter(VendorAdapter):
    """100% official Transsion (TECNO / Infinix / itel) v4 stock firmware adapter."""

    def __init__(
        self,
        vendor_id: str,
        *,
        play_csv_url: str = GOOGLE_PLAY_DEVICES_URL,
        build_seeds: Sequence[tuple[int, str]] = DEFAULT_ANDROID_BUILD_SEEDS,
        regions: Sequence[str] = DEFAULT_TRANSSION_REGIONS,
        max_probes_per_device: int = DEFAULT_MAX_PROBES_PER_DEVICE,
        request_delay_seconds: float = DEFAULT_REQUEST_DELAY_SECONDS,
        probe_workers: int = DEFAULT_PROBE_WORKERS,
        sleep_fn: Callable[[float], None] | None = None,
    ) -> None:
        key = vendor_id.strip().lower()
        if key not in SUPPORTED_TRANSSION_VENDORS:
            raise ValueError(
                f"Unsupported Transsion vendor_id {vendor_id!r}; expected one of {SUPPORTED_TRANSSION_VENDORS}"
            )
        self.vendor_id = key
        self.play_csv_url = play_csv_url
        self.build_seeds = tuple((int(ver), str(bid).strip()) for ver, bid in build_seeds)
        self.regions = tuple(str(r).strip().upper() for r in regions if str(r).strip())
        self.max_probes_per_device = max(1, int(max_probes_per_device))
        self.request_delay_seconds = max(0.0, float(request_delay_seconds))
        self.probe_workers = max(1, int(probe_workers))
        self._sleep_fn = sleep_fn

    def discover_devices(self, http: OfficialMetadataHTTP) -> list[AdapterDevice]:
        # Parse Play CSV preserving exact mixed-case `Device` (e.g. `TECNO-CK7n`) required by GOTA checkin.
        resp = http.get(self.play_csv_url)
        text = decode_supported_devices_bytes(resp.content)
        if not text.strip():
            return []

        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames:
            return []

        field_map: dict[str, str] = {}
        for col in reader.fieldnames:
            if col is None:
                continue
            field_map[col.lstrip("\ufeff").strip().lower()] = col

        brand_col = field_map.get("retail branding")
        marketing_col = field_map.get("marketing name")
        device_col = field_map.get("device")
        model_col = field_map.get("model")
        if not (brand_col and device_col and model_col):
            raise ValueError(f"Unexpected supported_devices.csv header: {reader.fieldnames!r}")

        allowed_brands = {_RE_WHITESPACE.sub(" ", b.strip()).lower() for b in TRANSSION_PLAY_BRANDS[self.vendor_id]}

        models_by_code: dict[str, list[str]] = {}
        raw_codes_by_code: dict[str, list[str]] = {}
        names_by_code: dict[str, list[str]] = {}

        for row in reader:
            raw_brand = _RE_WHITESPACE.sub(" ", (row.get(brand_col) or "").strip()).lower()
            if raw_brand not in allowed_brands:
                continue

            raw_device = (row.get(device_col) or "").strip()
            raw_model = _RE_WHITESPACE.sub(" ", (row.get(model_col) or "").strip())
            raw_marketing = (
                _RE_WHITESPACE.sub(" ", (row.get(marketing_col) or "").strip())
                if marketing_col
                else ""
            )
            if not raw_device or not raw_model:
                continue
            if raw_device.lower() == "__global__" or not _RE_VALID_CSV_CODENAME.fullmatch(raw_device):
                continue

            other_brand_re = _OTHER_TRANSSION_PREFIX_RE[self.vendor_id]
            if other_brand_re.match(raw_device) or (raw_marketing and other_brand_re.match(raw_marketing)):
                continue

            marketing_display = raw_marketing or raw_model
            if _is_excluded_category(
                marketing_name=marketing_display,
                codename=raw_device.lower(),
                model=raw_model,
                include_wearables=False,
            ):
                continue

            combined_label = f"{marketing_display} {raw_model} {raw_device}"
            if _RE_EXCLUDED_TRANSSION_ACCESSORY.search(combined_label):
                continue

            clean_model = _strip_brand_prefix(raw_model)
            seed = _strip_brand_prefix(raw_device) or clean_model
            if not seed:
                continue

            codename = canonical_code(self.vendor_id, seed)
            if not codename or codename == "__global__":
                continue

            models_by_code.setdefault(codename, [])
            raw_codes_by_code.setdefault(codename, [])
            names_by_code.setdefault(codename, [])

            if clean_model and clean_model not in models_by_code[codename]:
                models_by_code[codename].append(clean_model)
            if raw_device and raw_device not in raw_codes_by_code[codename]:
                raw_codes_by_code[codename].append(raw_device)
            if marketing_display and marketing_display not in names_by_code[codename]:
                names_by_code[codename].append(marketing_display)

        devices: list[AdapterDevice] = []
        for codename in sorted(models_by_code):
            models = models_by_code[codename]
            raw_codes = raw_codes_by_code[codename]
            candidates = names_by_code[codename]

            chosen_name = candidates[0] if candidates else (models[0] if models else codename)
            model_upper_set = {m.upper() for m in models}
            for cand in candidates:
                if _strip_brand_prefix(cand).upper() not in model_upper_set:
                    chosen_name = cand
                    break

            normalized_cand = _RE_TECNO_MOBILE_NAME.sub(
                "TECNO", chosen_name.replace("_", " ")
            ).strip()
            clean_label = format_marketing_name(
                {
                    "vendor": self.vendor_id,
                    "upstream_id": codename,
                    "codename": codename,
                    "name": normalized_cand,
                    "models": ",".join(models),
                }
            )
            brand_prefix = TRANSSION_DISPLAY_BRANDS[self.vendor_id]
            if not clean_label or clean_label.strip().lower() in ("tecno", "tecno mobile", "infinix", "itel"):
                clean_label = f"{brand_prefix} {models[0]}" if models else chosen_name
            elif not re.match(rf"^{re.escape(brand_prefix)}\b", clean_label, re.IGNORECASE):
                clean_label = f"{brand_prefix} {clean_label}"

            final_name = clean_label or f"{brand_prefix} {codename}"
            devices.append(
                AdapterDevice(
                    codename=codename,
                    brand_id=self.vendor_id,
                    marketing_name=final_name,
                    model_numbers=tuple(models),
                    boot_partition_type="init_boot",
                    has_vbmeta=1,
                    vendor_meta={
                        "models": list(models),
                        "raw_codenames": list(raw_codes),
                        "collect_priority": _transsion_collect_priority(
                            self.vendor_id, final_name, codename
                        ),
                    },
                )
            )
        return devices

    def _build_probe_targets(self, device: AdapterDevice) -> list[tuple[str, str]]:
        meta = device.vendor_meta if isinstance(device.vendor_meta, dict) else {}
        models = [
            _strip_brand_prefix(str(m))
            for m in (meta.get("models") or device.model_numbers or ())
            if str(m).strip()
        ]
        raw_codes = [
            str(c).strip() for c in (meta.get("raw_codenames") or ()) if str(c).strip()
        ]

        if not models and raw_codes:
            models = [_strip_brand_prefix(raw_codes[0])]
        if not raw_codes and models:
            prefix = TRANSSION_FINGERPRINT_BRANDS[self.vendor_id]
            raw_codes = [f"{prefix}-{models[0]}"]
        if not models or not raw_codes:
            fallback = _strip_brand_prefix(device.codename.removeprefix(f"{self.vendor_id}-"))
            if not fallback:
                return []
            prefix = TRANSSION_FINGERPRINT_BRANDS[self.vendor_id]
            return [(fallback, f"{prefix}-{fallback}")]

        targets: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for idx, model in enumerate(models):
            raw_code = raw_codes[idx] if idx < len(raw_codes) else raw_codes[0]
            pair = (model, raw_code)
            if pair not in seen:
                seen.add(pair)
                targets.append(pair)
        return targets

    def _probe_single_fingerprint(
        self,
        *,
        http: OfficialMetadataHTTP,
        fingerprint: str,
        model: str,
        region_hint: str,
        device: AdapterDevice,
    ) -> AdapterRelease | None:
        payload, headers = GoogleCheckinClient.build_checkin_request(
            fingerprint, model=model
        )
        try:
            resp = http.post(
                GOOGLE_CHECKIN_URL,
                content=payload,
                headers=headers,
            )
        except httpx.HTTPStatusError as exc:
            # 429 / 5xx after OfficialMetadataHTTP retries -> surface to collector for device backoff
            if exc.response.status_code in (400, 403, 404, 410):
                return None
            raise

        settings = GoogleCheckinClient.parse_checkin_response(resp.content)
        download_url = (settings.get("update_url") or "").strip()
        if not download_url:
            return None

        parsed_url = urlsplit(download_url)
        if (parsed_url.hostname or "").lower() != "android.googleapis.com" or not parsed_url.path.startswith(
            "/packages/ota-api/"
        ):
            return None

        try:
            validated_url = validate_official_download_url(
                self.vendor_id, download_url, resolve_dns=False
            )
        except ValueError:
            return None

        title = (settings.get("update_title") or "").strip() or PurePosixPath(
            parsed_url.path
        ).name.removesuffix(".zip")
        try:
            size_bytes = int(settings.get("update_size_value") or 0)
        except ValueError:
            size_bytes = 0

        is_incremental = not (title.startswith("Tcard_") or size_bytes > 1024 * 1024 * 1024)
        if is_incremental:
            return None
        if 0 < size_bytes < MIN_FULL_PACKAGE_BYTES:
            return None

        raw_sdk = settings.get("update_target_sdk_level")
        try:
            sdk_level = int(raw_sdk) if raw_sdk else None
        except ValueError:
            sdk_level = None

        parsed_ver = parse_transsion_version(title, brand=self.vendor_id)
        raw_clean_ver = str(parsed_ver.get("clean_version") or title).strip()
        if not raw_clean_ver:
            return None

        canon_ver = canonical_version_str(raw_clean_ver)
        model_code = str(parsed_ver.get("model_code") or model).strip() or model
        parsed_region = str(parsed_ver.get("region_code") or "").strip()
        naming_style = str(parsed_ver.get("naming_style") or "raw")
        has_explicit_region = bool(
            parsed_region
            and (
                naming_style == "modern_os"
                or (naming_style == "classic_project" and f"-{parsed_region}-" in title)
            )
        )
        region_code = parsed_region if has_explicit_region else (region_hint or parsed_region or "GL")

        android_ver = _normalize_sdk_android_version(
            parsed_ver.get("android_version"), sdk_level
        )
        os_name = str(parsed_ver.get("os_name") or BRAND_TO_OS[self.vendor_id])
        description = settings.get("update_description")
        os_version = parsed_ver.get("os_version") or _extract_os_version_from_description(
            description
        )
        pub_date = iso_date(parsed_ver.get("build_date"))
        if not pub_date and settings.get("update_timestamp"):
            try:
                ts_val = int(settings["update_timestamp"])
                if ts_val > 0:
                    pub_date = datetime.datetime.fromtimestamp(
                        ts_val, tz=datetime.timezone.utc
                    ).strftime("%Y-%m-%d")
            except ValueError:
                pass

        m_sha1 = _RE_OTA_SHA1.search(parsed_url.path)
        ota_sha1 = m_sha1.group(1).lower() if m_sha1 else None

        vendor_fields: dict[str, Any] = {
            "precondition_fingerprint": fingerprint,
        }
        if ota_sha1:
            vendor_fields["ota_sha1"] = ota_sha1
        if parsed_ver.get("board_code"):
            vendor_fields["board_code"] = parsed_ver["board_code"]
        if parsed_ver.get("build_number"):
            vendor_fields["build_number"] = parsed_ver["build_number"]
        if parsed_ver.get("carrier_code"):
            vendor_fields["carrier_code"] = parsed_ver["carrier_code"]

        return AdapterRelease(
            device_codename=device.codename,
            model_code=model_code,
            region_code=region_code,
            version_str=canon_ver,
            android_version=android_ver,
            package_type="recovery_zip",
            original_official_url=validated_url,
            fast_cdn_url=validated_url,
            md5_or_sha256=None,
            published_date=pub_date,
            file_size_bytes=size_bytes if size_bytes > 0 else None,
            file_size_str=format_file_size(size_bytes) if size_bytes > 0 else None,
            delivery_mode="direct_cdn",
            verification_status="VERIFIED_IMMUTABLE",
            display_version=str(parsed_ver.get("display_version") or raw_clean_ver),
            raw_version=title,
            os_name=os_name,
            os_version=os_version,
            original_filename=_build_safe_filename(title, validated_url),
            vendor_fields=vendor_fields,
            sources={"google_ota_checkin": GOOGLE_CHECKIN_URL},
            availability="available",
        )

    def fetch_releases(
        self,
        device: AdapterDevice,
        http: OfficialMetadataHTTP,
    ) -> list[AdapterRelease]:
        targets = self._build_probe_targets(device)
        if not targets:
            return []

        fp_brand = TRANSSION_FINGERPRINT_BRANDS[self.vendor_id]
        probe_jobs: list[tuple[str, str, str]] = []
        # Interleave across regions first so every region gets sampled even under probe cap
        for android_ver, build_id in self.build_seeds:
            for region in self.regions:
                for model, raw_code in targets:
                    if len(probe_jobs) >= self.max_probes_per_device:
                        break
                    fingerprint = (
                        f"{fp_brand}/{model}-{region}/{raw_code}:{android_ver}/{build_id}/1:user/release-keys"
                    )
                    probe_jobs.append((model, region, fingerprint))
                if len(probe_jobs) >= self.max_probes_per_device:
                    break
            if len(probe_jobs) >= self.max_probes_per_device:
                break

        if not probe_jobs:
            return []

        releases: list[AdapterRelease] = []
        seen_urls: set[str] = set()
        seen_identities: set[tuple[str, str, str]] = set()

        def _record(rel: AdapterRelease | None) -> None:
            if rel is None or rel.fast_cdn_url in seen_urls:
                return
            ikey = (rel.model_code, rel.region_code, rel.canonical_version)
            if ikey in seen_identities:
                return
            seen_urls.add(rel.fast_cdn_url)
            seen_identities.add(ikey)
            releases.append(rel)

        is_mock = isinstance(getattr(http._client, "_transport", None), httpx.MockTransport)
        if self.probe_workers > 1 and self._sleep_fn is None and not is_mock and len(probe_jobs) > 1:
            workers = min(self.probe_workers, len(probe_jobs))
            with ThreadPoolExecutor(max_workers=workers) as executor:
                results = list(
                    executor.map(
                        lambda item: self._probe_single_fingerprint(
                            http=http,
                            fingerprint=item[2],
                            model=item[0],
                            region_hint=item[1],
                            device=device,
                        ),
                        probe_jobs,
                    )
                )
            for rel in results:
                _record(rel)
            return releases

        sleep_fn = self._sleep_fn or _default_sleep_fn(http)
        for idx, (model, region, fingerprint) in enumerate(probe_jobs):
            if idx > 0 and self.request_delay_seconds > 0:
                sleep_fn(self.request_delay_seconds)
            _record(
                self._probe_single_fingerprint(
                    http=http,
                    fingerprint=fingerprint,
                    model=model,
                    region_hint=region,
                    device=device,
                )
            )

        return releases


__all__ = [
    "DEFAULT_ANDROID_BUILD_SEEDS",
    "DEFAULT_MAX_PROBES_PER_DEVICE",
    "DEFAULT_PROBE_WORKERS",
    "DEFAULT_REQUEST_DELAY_SECONDS",
    "DEFAULT_TRANSSION_REGIONS",
    "SUPPORTED_TRANSSION_VENDORS",
    "TRANSSION_FINGERPRINT_BRANDS",
    "TRANSSION_PLAY_BRANDS",
    "TranssionAdapter",
]
