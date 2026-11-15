"""FindMyROM firmware partition extraction library."""

from typing import Any

__all__ = [
    "extract_partitions_via_range",
    "validate_and_compress_partition",
    "VaultClient",
    "run_extraction_batch",
]


def __getattr__(name: str) -> Any:
    if name == "extract_partitions_via_range":
        from .range_extractor import extract_partitions_via_range

        return extract_partitions_via_range
    if name == "validate_and_compress_partition":
        from .patcher import validate_and_compress_partition

        return validate_and_compress_partition
    if name == "VaultClient":
        from .vault_client import VaultClient

        return VaultClient
    if name == "run_extraction_batch":
        from .scheduler import run_extraction_batch

        return run_extraction_batch
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals().keys()) | set(__all__))
