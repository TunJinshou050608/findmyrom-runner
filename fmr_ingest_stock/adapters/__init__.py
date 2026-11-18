"""Official v4 stock-firmware vendor adapters and declarative config loader."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .base import (
    OFFICIAL_METADATA_HOSTS,
    AdapterDevice,
    AdapterRelease,
    MetadataResponse,
    OfficialMetadataHTTP,
    VendorAdapter,
    validate_official_metadata_url,
)
from .google import GoogleAdapter
from .google_play_devices import (
    GOOGLE_PLAY_DEVICES_URL,
    PlayCatalogDevice,
    PlayDeviceRecord,
    fetch_google_play_devices,
    group_play_devices_by_codename,
    parse_google_play_devices_csv,
)

DEFAULT_VENDORS_YAML_PATH = Path(__file__).resolve().parents[2] / "config" / "vendors.yaml"


@dataclass(frozen=True)
class VendorConfig:
    vendor_id: str
    enabled: bool
    adapter: str | None
    status: str
    tier: str = "official"
    bucket: int | None = None
    max_collect_seconds: int = 600
    verify_workers: int = 4
    max_devices: int | None = None
    display_name: str = ""

    def __post_init__(self) -> None:
        if not self.display_name:
            object.__setattr__(self, "display_name", self.vendor_id.capitalize())


ADAPTER_REGISTRY: dict[str, type[VendorAdapter]] = {
    "google": GoogleAdapter,
}


def load_vendor_configs(path: str | Path | None = None) -> dict[str, VendorConfig]:
    """Load declarative vendor configurations using safe YAML parsing."""
    cfg_path = Path(path) if path is not None else DEFAULT_VENDORS_YAML_PATH
    raw_data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    if not isinstance(raw_data, dict) or not isinstance(raw_data.get("vendors"), dict):
        raise ValueError(f"Invalid vendors configuration in {cfg_path}")

    configs: dict[str, VendorConfig] = {}
    for raw_id, entry in raw_data["vendors"].items():
        if not isinstance(entry, dict):
            raise ValueError(f"Invalid vendor entry for {raw_id!r} in {cfg_path}")
        vendor_id = str(raw_id).strip().lower()
        enabled = bool(entry.get("enabled", False))
        adapter_val = entry.get("adapter")
        adapter_name = str(adapter_val).strip().lower() if adapter_val else None
        status = str(entry.get("status") or ("active" if enabled else "disabled")).strip()
        tier = str(entry.get("tier") or "official").strip()
        raw_bucket = entry.get("bucket")
        bucket = int(raw_bucket) if raw_bucket is not None else None
        if bucket is not None and not (0 <= bucket <= 11):
            raise ValueError(f"Vendor {vendor_id!r} bucket must be in 0..11, got {bucket}")
        max_collect_seconds = int(entry.get("max_collect_seconds", 600))
        verify_workers = int(entry.get("verify_workers", 4))
        raw_max_devices = entry.get("max_devices")
        max_devices = int(raw_max_devices) if raw_max_devices is not None else None
        if max_devices is not None and max_devices <= 0:
            raise ValueError(
                f"Vendor {vendor_id!r} max_devices must be positive, got {max_devices}"
            )
        display_name = str(entry.get("display_name") or vendor_id.capitalize()).strip()

        if status == "suppressed_closed_bootloader" and enabled:
            raise ValueError(f"Suppressed vendor {vendor_id!r} cannot be enabled")

        configs[vendor_id] = VendorConfig(
            vendor_id=vendor_id,
            enabled=enabled,
            adapter=adapter_name,
            status=status,
            tier=tier,
            bucket=bucket,
            max_collect_seconds=max_collect_seconds,
            verify_workers=verify_workers,
            max_devices=max_devices,
            display_name=display_name,
        )
    return configs


def get_adapter(vendor_id: str, **kwargs: Any) -> VendorAdapter:
    """Instantiate a registered official vendor adapter via lazy module loading."""
    key = vendor_id.strip().lower()
    override_cls = ADAPTER_REGISTRY.get(key)
    if override_cls is not None and (key != "google" or override_cls is not GoogleAdapter):
        return override_cls(**kwargs)

    if key == "google":
        from .google import GoogleAdapter as _GoogleAdapter

        return _GoogleAdapter(**kwargs)
    if key == "samsung":
        from .samsung import SamsungAdapter

        return SamsungAdapter(**kwargs)
    if key in ("asus", "rog"):
        from .asus import AsusAdapter

        return AsusAdapter(**kwargs)
    if key == "vivo":
        from .vivo import VivoAdapter

        return VivoAdapter(**kwargs)
    if key == "xiaomi":
        from .xiaomi import XiaomiAdapter

        return XiaomiAdapter(**kwargs)
    if key in ("oneplus", "oppo", "realme"):
        from .oplus import OPlusAdapter

        return OPlusAdapter(key, **kwargs)
    if key in ("tecno", "infinix", "itel"):
        from .transsion import TranssionAdapter

        return TranssionAdapter(key, **kwargs)

    raise KeyError(f"No official adapter registered for vendor {vendor_id!r}")


__all__ = [
    "ADAPTER_REGISTRY",
    "AdapterDevice",
    "AdapterRelease",
    "DEFAULT_VENDORS_YAML_PATH",
    "GOOGLE_PLAY_DEVICES_URL",
    "GoogleAdapter",
    "MetadataResponse",
    "OFFICIAL_METADATA_HOSTS",
    "OfficialMetadataHTTP",
    "PlayCatalogDevice",
    "PlayDeviceRecord",
    "VendorAdapter",
    "VendorConfig",
    "fetch_google_play_devices",
    "get_adapter",
    "group_play_devices_by_codename",
    "load_vendor_configs",
    "parse_google_play_devices_csv",
    "validate_official_metadata_url",
]
