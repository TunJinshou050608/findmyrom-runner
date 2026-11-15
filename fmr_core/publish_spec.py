"""Canonical V1 stock-firmware publish contract and validation."""

from __future__ import annotations

import copy
from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any
from urllib.parse import urlsplit

from .extraction_policy import load_extraction_matrix, package_priority

RESTRICTED_BRANDS: frozenset[str] = frozenset({"huawei", "honor"})
SUPPORTED_BRANDS: frozenset[str] = (
    frozenset(load_extraction_matrix().get("official_cdn_hosts", {}).keys())
    | frozenset({"other"})
) - RESTRICTED_BRANDS

VALID_BOOT_PARTITIONS: frozenset[str] = frozenset({"boot", "init_boot"})
VALID_VERIFICATION_STATUSES: frozenset[str] = frozenset(
    {"VERIFIED_IMMUTABLE", "VERIFIED_RESOLVER", "VERIFIED_R2_BUNDLE"}
)

_RE_CODENAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_RE_VERSION_STR = re.compile(r"^[A-Za-z0-9._/-]{1,64}$")
_RE_CHECKSUM = re.compile(r"^(?:[0-9a-f]{32}|[0-9a-f]{64})$")
_RE_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_RE_WHITESPACE_OR_CTRL = re.compile(r"[\s\x00-\x1f\x7f]")


def canonical_version_str(raw: str) -> str:
    """Canonicalize a build version string using safe 64-char truncation."""
    cleaned = str(raw).strip()
    if not cleaned:
        raise ValueError("version_str must be non-empty")
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", cleaned)
    if safe != cleaned or len(safe) > 64:
        digest = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()[:12]
        safe = f"{safe[:49]}-{digest}"
    return safe


def _validate_https_url(url: str, field_name: str) -> None:
    if not isinstance(url, str) or not url.startswith("https://"):
        raise ValueError(f"{field_name} must start with 'https://': {url!r}")
    if _RE_WHITESPACE_OR_CTRL.search(url) or any(ord(ch) < 32 or ord(ch) == 127 for ch in url):
        raise ValueError(f"{field_name} must not contain whitespace or control characters: {url!r}")
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError(f"{field_name} must include a valid HTTPS hostname: {url!r}")


def _validate_codename(codename: str, field_name: str = "codename") -> None:
    if not isinstance(codename, str) or codename == "__global__" or not _RE_CODENAME.fullmatch(codename):
        raise ValueError(
            f"{field_name} must match ^[a-z0-9][a-z0-9_-]{{0,63}}$ and not be '__global__': {codename!r}"
        )


@dataclass(frozen=True)
class DeviceSpec:
    codename: str
    brand_id: str
    marketing_name: str
    model_numbers: tuple[str, ...] = ()
    soc_name: str | None = None
    boot_partition_type: str = "init_boot"
    has_vbmeta: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.model_numbers, tuple):
            if isinstance(self.model_numbers, (list, set)):
                object.__setattr__(self, "model_numbers", tuple(self.model_numbers))
            else:
                raise ValueError("model_numbers must be a tuple or list of strings")
        self.validate()

    def validate(self) -> None:
        _validate_codename(self.codename, "device.codename")
        if not isinstance(self.brand_id, str) or not self.brand_id:
            raise ValueError("device.brand_id must be a non-empty string")
        if self.brand_id.strip().lower() in RESTRICTED_BRANDS:
            raise ValueError(f"Restricted brand_id is prohibited: {self.brand_id!r}")
        if self.brand_id not in SUPPORTED_BRANDS:
            raise ValueError(f"Unsupported brand_id: {self.brand_id!r}")
        if not isinstance(self.marketing_name, str) or not self.marketing_name.strip():
            raise ValueError("device.marketing_name must be a non-empty string")
        if any(not isinstance(m, str) or not m.strip() for m in self.model_numbers):
            raise ValueError("device.model_numbers entries must be non-empty strings")
        if self.soc_name is not None and (not isinstance(self.soc_name, str) or not self.soc_name.strip()):
            raise ValueError("device.soc_name must be None or a non-empty string")
        if self.boot_partition_type not in VALID_BOOT_PARTITIONS:
            raise ValueError(
                f"device.boot_partition_type must be in {sorted(VALID_BOOT_PARTITIONS)}: {self.boot_partition_type!r}"
            )
        if isinstance(self.has_vbmeta, bool) or self.has_vbmeta not in (0, 1):
            raise ValueError(f"device.has_vbmeta must be 0 or 1, got {self.has_vbmeta!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "codename": self.codename,
            "brand_id": self.brand_id,
            "marketing_name": self.marketing_name,
            "model_numbers": list(self.model_numbers),
            "soc_name": self.soc_name,
            "boot_partition_type": self.boot_partition_type,
            "has_vbmeta": self.has_vbmeta,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DeviceSpec:
        if not isinstance(data, dict):
            raise ValueError("DeviceSpec data must be a dict")
        return cls(
            codename=data["codename"],
            brand_id=data["brand_id"],
            marketing_name=data["marketing_name"],
            model_numbers=tuple(data.get("model_numbers") or ()),
            soc_name=data.get("soc_name"),
            boot_partition_type=data.get("boot_partition_type", "init_boot"),
            has_vbmeta=data.get("has_vbmeta", 1),
        )


@dataclass(frozen=True)
class ReleaseSpec:
    device_codename: str
    model_code: str
    region_code: str
    version_str: str
    android_version: str
    package_type: str
    original_official_url: str
    fast_cdn_url: str
    file_size_bytes: int | None = None
    file_size_str: str | None = None
    md5_or_sha256: str | None = None
    published_date: str | None = None
    delivery_mode: str = "direct_cdn"
    verification_status: str = "VERIFIED_IMMUTABLE"
    resolver_meta: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.resolver_meta is not None:
            if not isinstance(self.resolver_meta, dict):
                raise ValueError("release.resolver_meta must be None or a dict")
            object.__setattr__(self, "resolver_meta", copy.deepcopy(self.resolver_meta))
        self.validate()

    def validate(self) -> None:
        _validate_codename(self.device_codename, "release.device_codename")
        if not isinstance(self.model_code, str):
            raise ValueError("release.model_code must be a string")
        if not isinstance(self.region_code, str) or not self.region_code.strip():
            raise ValueError("release.region_code must be a non-empty string")
        if not isinstance(self.version_str, str) or not _RE_VERSION_STR.fullmatch(self.version_str):
            raise ValueError(
                f"release.version_str must be 1..64 chars matching [A-Za-z0-9._/-]: {self.version_str!r}"
            )
        if not isinstance(self.android_version, str) or not self.android_version.strip():
            raise ValueError("release.android_version must be a non-empty string")
        allowed_packages = set(package_priority())
        if self.package_type not in allowed_packages:
            raise ValueError(
                f"release.package_type must be in {sorted(allowed_packages)}: {self.package_type!r}"
            )
        _validate_https_url(self.original_official_url, "release.original_official_url")
        _validate_https_url(self.fast_cdn_url, "release.fast_cdn_url")
        if self.file_size_bytes is not None:
            if isinstance(self.file_size_bytes, bool) or not isinstance(self.file_size_bytes, int) or self.file_size_bytes < 0:
                raise ValueError(f"release.file_size_bytes must be a non-negative integer: {self.file_size_bytes!r}")
        if self.file_size_str is not None and (not isinstance(self.file_size_str, str) or not self.file_size_str.strip()):
            raise ValueError("release.file_size_str must be None or a non-empty string")
        if self.md5_or_sha256 is not None:
            if not isinstance(self.md5_or_sha256, str) or not _RE_CHECKSUM.fullmatch(self.md5_or_sha256):
                raise ValueError(
                    f"release.md5_or_sha256 must match ^[0-9a-f]{{32}}$ or ^[0-9a-f]{{64}}$: {self.md5_or_sha256!r}"
                )
        if self.published_date is not None:
            if not isinstance(self.published_date, str) or not _RE_ISO_DATE.fullmatch(self.published_date):
                raise ValueError(
                    f"release.published_date must match ^\\d{{4}}-\\d{{2}}-\\d{{2}}$: {self.published_date!r}"
                )
        if not isinstance(self.delivery_mode, str) or not self.delivery_mode.strip():
            raise ValueError("release.delivery_mode must be a non-empty string")
        if self.verification_status not in VALID_VERIFICATION_STATUSES:
            raise ValueError(
                f"release.verification_status must be in {sorted(VALID_VERIFICATION_STATUSES)}: {self.verification_status!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "device_codename": self.device_codename,
            "model_code": self.model_code,
            "region_code": self.region_code,
            "version_str": self.version_str,
            "android_version": self.android_version,
            "package_type": self.package_type,
            "original_official_url": self.original_official_url,
            "fast_cdn_url": self.fast_cdn_url,
            "file_size_bytes": self.file_size_bytes,
            "file_size_str": self.file_size_str,
            "md5_or_sha256": self.md5_or_sha256,
            "published_date": self.published_date,
            "delivery_mode": self.delivery_mode,
            "verification_status": self.verification_status,
            "resolver_meta": copy.deepcopy(self.resolver_meta) if self.resolver_meta is not None else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ReleaseSpec:
        if not isinstance(data, dict):
            raise ValueError("ReleaseSpec data must be a dict")
        return cls(
            device_codename=data["device_codename"],
            model_code=data["model_code"],
            region_code=data["region_code"],
            version_str=data["version_str"],
            android_version=data["android_version"],
            package_type=data["package_type"],
            original_official_url=data["original_official_url"],
            fast_cdn_url=data["fast_cdn_url"],
            file_size_bytes=data.get("file_size_bytes"),
            file_size_str=data.get("file_size_str"),
            md5_or_sha256=data.get("md5_or_sha256"),
            published_date=data.get("published_date"),
            delivery_mode=data.get("delivery_mode", "direct_cdn"),
            verification_status=data.get("verification_status", "VERIFIED_IMMUTABLE"),
            resolver_meta=data.get("resolver_meta"),
        )


@dataclass(frozen=True)
class EnrichmentSpec:
    display_version: str | None = None
    raw_version: str | None = None
    os_name: str | None = None
    os_version: str | None = None
    security_patch: str | None = None
    original_filename: str | None = None
    vendor_fields: dict[str, Any] | None = None
    sources: dict[str, str] | None = None
    availability: str | None = None
    unavailable_reason: str | None = None

    def __post_init__(self) -> None:
        if self.vendor_fields is not None:
            if not isinstance(self.vendor_fields, dict):
                raise ValueError("enrichment.vendor_fields must be None or a dict")
            object.__setattr__(self, "vendor_fields", copy.deepcopy(self.vendor_fields))
        if self.sources is not None:
            if not isinstance(self.sources, dict) or any(
                not isinstance(k, str) or not isinstance(v, str) for k, v in self.sources.items()
            ):
                raise ValueError("enrichment.sources must be None or dict[str, str]")
            object.__setattr__(self, "sources", dict(self.sources))
        self.validate()

    def validate(self) -> None:
        if self.security_patch is not None:
            if not isinstance(self.security_patch, str) or not _RE_ISO_DATE.fullmatch(self.security_patch):
                raise ValueError(
                    f"enrichment.security_patch must match ^\\d{{4}}-\\d{{2}}-\\d{{2}}$: {self.security_patch!r}"
                )

    def to_dict(self) -> dict[str, Any]:
        return {
            "display_version": self.display_version,
            "raw_version": self.raw_version,
            "os_name": self.os_name,
            "os_version": self.os_version,
            "security_patch": self.security_patch,
            "original_filename": self.original_filename,
            "vendor_fields": copy.deepcopy(self.vendor_fields) if self.vendor_fields is not None else None,
            "sources": dict(self.sources) if self.sources is not None else None,
            "availability": self.availability,
            "unavailable_reason": self.unavailable_reason,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EnrichmentSpec:
        if not isinstance(data, dict):
            raise ValueError("EnrichmentSpec data must be a dict")
        return cls(
            display_version=data.get("display_version"),
            raw_version=data.get("raw_version"),
            os_name=data.get("os_name"),
            os_version=data.get("os_version"),
            security_patch=data.get("security_patch"),
            original_filename=data.get("original_filename"),
            vendor_fields=data.get("vendor_fields"),
            sources=data.get("sources"),
            availability=data.get("availability"),
            unavailable_reason=data.get("unavailable_reason"),
        )


@dataclass(frozen=True)
class PublishSpecV1:
    device: DeviceSpec
    release: ReleaseSpec
    enrichment: EnrichmentSpec | None = None
    spec_version: int = 1

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if isinstance(self.spec_version, bool) or self.spec_version != 1:
            raise ValueError(f"PublishSpecV1.spec_version must be 1, got {self.spec_version!r}")
        if not isinstance(self.device, DeviceSpec):
            raise ValueError("PublishSpecV1.device must be a DeviceSpec instance")
        if not isinstance(self.release, ReleaseSpec):
            raise ValueError("PublishSpecV1.release must be a ReleaseSpec instance")
        if self.enrichment is not None and not isinstance(self.enrichment, EnrichmentSpec):
            raise ValueError("PublishSpecV1.enrichment must be None or an EnrichmentSpec instance")
        self.device.validate()
        self.release.validate()
        if self.enrichment is not None:
            self.enrichment.validate()
        if self.device.codename != self.release.device_codename:
            raise ValueError(
                f"device.codename ({self.device.codename!r}) must match release.device_codename ({self.release.device_codename!r})"
            )

    @property
    def identity_tuple(self) -> tuple[str, str, str, str, str]:
        effective_model = self.release.model_code.strip() or self.release.device_codename
        return (
            self.release.device_codename,
            effective_model,
            self.release.region_code,
            self.release.version_str,
            self.release.package_type,
        )

    @property
    def identity_key(self) -> str:
        return json.dumps(self.identity_tuple, separators=(",", ":"))

    @property
    def spec_sha256(self) -> str:
        payload = {
            "spec_version": self.spec_version,
            "device": self.device.to_dict(),
            "release": self.release.to_dict(),
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def to_firmware_metadata_dict(self) -> dict[str, Any] | None:
        if self.enrichment is None:
            return None
        android_ver = (
            None
            if self.release.android_version.strip().lower() == "unknown"
            else self.release.android_version
        )
        doc: dict[str, Any] = {
            "schema": 1,
            "vendor": self.device.brand_id,
            "display_version": self.enrichment.display_version or self.release.version_str,
            "raw_version": self.enrichment.raw_version or self.release.version_str,
            "android_version": android_ver,
            "os_name": self.enrichment.os_name,
            "os_version": self.enrichment.os_version,
            "published_date": self.release.published_date,
            "security_patch": self.enrichment.security_patch,
            "original_filename": self.enrichment.original_filename,
            "file_size": self.release.file_size_str,
            "vendor_fields": copy.deepcopy(self.enrichment.vendor_fields) if self.enrichment.vendor_fields else {},
            "sources": dict(self.enrichment.sources) if self.enrichment.sources else {},
        }
        if self.enrichment.availability is not None:
            doc["availability"] = self.enrichment.availability
        if self.enrichment.unavailable_reason is not None:
            doc["unavailable_reason"] = self.enrichment.unavailable_reason
        return doc

    @property
    def enrichment_sha256(self) -> str | None:
        meta = self.to_firmware_metadata_dict()
        if meta is None:
            return None
        canonical = json.dumps(meta, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "spec_version": self.spec_version,
            "device": self.device.to_dict(),
            "release": self.release.to_dict(),
            "enrichment": self.enrichment.to_dict() if self.enrichment is not None else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PublishSpecV1:
        if not isinstance(data, dict):
            raise ValueError("PublishSpecV1 data must be a dict")
        enrichment_raw = data.get("enrichment")
        return cls(
            spec_version=data.get("spec_version", 1),
            device=DeviceSpec.from_dict(data["device"]),
            release=ReleaseSpec.from_dict(data["release"]),
            enrichment=EnrichmentSpec.from_dict(enrichment_raw) if enrichment_raw is not None else None,
        )


__all__ = [
    "DeviceSpec",
    "EnrichmentSpec",
    "PublishSpecV1",
    "RESTRICTED_BRANDS",
    "ReleaseSpec",
    "SUPPORTED_BRANDS",
    "VALID_BOOT_PARTITIONS",
    "VALID_VERIFICATION_STATUSES",
    "canonical_version_str",
]
