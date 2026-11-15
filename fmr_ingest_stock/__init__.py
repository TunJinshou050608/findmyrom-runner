"""Official firmware metadata ingestion (SP02)."""

from typing import Any

__all__ = [
    "rewrite_to_fast_cdn",
    "sync_devices",
    "sync_firmwares",
    "validate_official_download_url",
]


def __getattr__(name: str) -> Any:
    if name in ("rewrite_to_fast_cdn", "validate_official_download_url"):
        from . import cdn_rewriter

        return getattr(cdn_rewriter, name)
    if name == "sync_devices":
        from .device_sync import sync_devices

        return sync_devices
    if name == "sync_firmwares":
        from .firmware_sync import sync_firmwares

        return sync_firmwares
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals().keys()) | set(__all__))
