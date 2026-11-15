"""Only complete, static ZIP sources can enter the cloud Range extractor."""

from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

_MATRIX_PATH = Path(__file__).with_name("contracts") / "extraction_matrix.v1.json"


@lru_cache(maxsize=1)
def load_extraction_matrix() -> dict[str, Any]:
    """Load the canonical V1 extraction matrix contract."""
    data = json.loads(_MATRIX_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError(f"Invalid extraction matrix contract at {_MATRIX_PATH}")
    return data


def partition_limits() -> dict[str, tuple[int, int, bytes]]:
    """Return {partition_name: (min_bytes, max_bytes, magic_bytes)} from V1 contract."""
    raw = load_extraction_matrix().get("partition_limits", {})
    return {
        name: (
            int(spec["min_bytes"]),
            int(spec["max_bytes"]),
            str(spec["magic_ascii"]).encode("ascii"),
        )
        for name, spec in raw.items()
    }


def official_cdn_hosts(brand: str) -> tuple[str, ...]:
    """Return allowed official CDN host suffixes for a brand."""
    key = str(brand or "").strip().lower()
    hosts = load_extraction_matrix().get("official_cdn_hosts", {}).get(key, ())
    return tuple(hosts)


def package_priority() -> tuple[str, ...]:
    """Return ordered firmware package types from highest to lowest priority."""
    return tuple(load_extraction_matrix().get("package_priority", ()))


EXTRACTION_BRANDS: frozenset[str] = frozenset(
    load_extraction_matrix()["batch_vault"]["brands"]
)
EXTRACTION_PACKAGES: frozenset[str] = frozenset(
    load_extraction_matrix()["batch_vault"]["packages"]
)


def extraction_eligible(brand: str, package: str, url: str) -> bool:
    if brand not in EXTRACTION_BRANDS or package not in EXTRACTION_PACKAGES:
        return False
    source = urlsplit(url)
    return (
        source.scheme == "https"
        and bool(source.hostname)
        and source.hostname != "findmyrom.com"
        and source.path.lower().endswith(".zip")
        and not source.query
        and not source.fragment
        and not source.username
        and not source.password
    )


__all__ = [
    "EXTRACTION_BRANDS",
    "EXTRACTION_PACKAGES",
    "extraction_eligible",
    "load_extraction_matrix",
    "official_cdn_hosts",
    "package_priority",
    "partition_limits",
]
