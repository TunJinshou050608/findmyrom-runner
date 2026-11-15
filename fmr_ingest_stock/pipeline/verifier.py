"""Bounded 64-byte HTTP Range verifier for official stock firmware packages."""

from __future__ import annotations

from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import re
from typing import Literal
from urllib.parse import urljoin

import httpx

from fmr_ingest_stock.adapters.base import AdapterRelease
from fmr_ingest_stock.cdn_rewriter import (
    DNSTransientError,
    validate_official_download_url,
)

DEFAULT_MIN_FULL_BYTES = 50 * 1024 * 1024  # 50 MiB
DEFAULT_MIN_INCREMENTAL_BYTES = 1 * 1024 * 1024  # 1 MiB
DEAD_UPSTREAM_TTL_SECONDS = 7 * 24 * 3600  # 7 days
BASE_DEFERRED_BACKOFF_SECONDS = 900  # 15 minutes
MAX_DEFERRED_BACKOFF_SECONDS = 24 * 3600  # 24 hours

FORBIDDEN_CONTENT_TYPE_PREFIXES: tuple[str, ...] = (
    "text/html",
    "application/json",
    "application/xml",
    "text/plain",
)

ZIP_MAGIC = b"PK\x03\x04"
GZIP_MAGIC = b"\x1f\x8b"

_RE_CONTENT_RANGE = re.compile(r"^bytes\s+(\d+)-(\d+)/(\d+|\*)$", re.IGNORECASE)
_RE_FMR_RESOLVER_URL = re.compile(
    r"^https://findmyrom\.com/(?:stock/refresh|sony/bundles)/[0-9a-f]{64}$"
)
_RE_ETAG_MD5_HEX = re.compile(r"^[0-9a-fA-F]{32}$")

VerifyClassification = Literal["VERIFIED", "DEAD_UPSTREAM", "DEFERRED_VERIFY"]


class UnofficialDownloadURLError(ValueError):
    """Raised when a firmware URL or redirect hop fails official OEM host validation."""


@dataclass(frozen=True)
class ProbeOutcome:
    status: VerifyClassification
    release: AdapterRelease
    file_size_bytes: int | None = None
    file_size_str: str | None = None
    final_url: str | None = None
    http_status: int | None = None
    reason: str | None = None
    retry_after_seconds: int = 0
    rejected_unofficial: bool = False


def format_file_size(num_bytes: int) -> str:
    """Format exact byte count into human-readable string (e.g. '2.38 GB', '845.2 MB')."""
    if num_bytes < 0:
        raise ValueError(f"num_bytes must be non-negative, got {num_bytes}")
    gib = 1024**3
    mib = 1024**2
    kib = 1024
    if num_bytes >= gib:
        return f"{num_bytes / gib:.2f} GB"
    if num_bytes >= mib:
        return f"{num_bytes / mib:.1f} MB"
    if num_bytes >= kib:
        return f"{num_bytes / kib:.1f} KB"
    return f"{num_bytes} B"


def compute_deferred_backoff(failure_count: int) -> int:
    exp = max(0, min(int(failure_count), 6))
    return min(MAX_DEFERRED_BACKOFF_SECONDS, BASE_DEFERRED_BACKOFF_SECONDS * (2**exp))


def _expected_magic_for_package(package_type: str) -> bytes | None:
    pkg = package_type.strip().lower()
    if pkg.endswith("_zip"):
        return ZIP_MAGIC
    if pkg.endswith("_tgz"):
        return GZIP_MAGIC
    return None


def _extract_total_size(status_code: int, headers: httpx.Headers) -> int | None:
    if status_code == 206:
        cr = headers.get("content-range", "").strip()
        m = _RE_CONTENT_RANGE.match(cr)
        if m and m.group(3) != "*":
            return int(m.group(3))
        return None
    if status_code == 200:
        cl = headers.get("content-length", "").strip()
        if cl.isdigit():
            return int(cl)
    return None


def _extract_etag_md5(headers: httpx.Headers) -> str | None:
    raw_etag = headers.get("etag", "").strip()
    if not raw_etag:
        return None
    if raw_etag[:2].lower() == "w/":
        raw_etag = raw_etag[2:].strip()
    if len(raw_etag) >= 2 and raw_etag[0] == '"' and raw_etag[-1] == '"':
        raw_etag = raw_etag[1:-1].strip()
    else:
        raw_etag = raw_etag.strip('"').strip()
    if _RE_ETAG_MD5_HEX.fullmatch(raw_etag):
        return raw_etag.lower()
    return None


def _parse_retry_after_seconds(headers: httpx.Headers) -> int | None:
    raw = headers.get("retry-after", "").strip()
    if raw.isdigit():
        return max(60, min(86400, int(raw)))
    return None


class RangeProbeVerifier:
    """Verifies direct CDN firmware archives using a bounded `Range: bytes=0-63` probe."""

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        verify_workers: int = 4,
        resolve_dns: bool = True,
        min_full_bytes: int = DEFAULT_MIN_FULL_BYTES,
        min_incremental_bytes: int = DEFAULT_MIN_INCREMENTAL_BYTES,
        max_redirects: int = 3,
        timeout: float = 20.0,
    ) -> None:
        self.verify_workers = max(1, int(verify_workers))
        self.resolve_dns = bool(resolve_dns)
        self.min_full_bytes = int(min_full_bytes)
        self.min_incremental_bytes = int(min_incremental_bytes)
        self.max_redirects = max(0, int(max_redirects))

        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(timeout),
            follow_redirects=False,
            headers={
                "User-Agent": "FindMyROM-RangeVerifier/4.0 (+https://findmyrom.com)",
            },
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> RangeProbeVerifier:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def validate_url(self, brand: str, url: str) -> str:
        """Strictly validate a download or redirect URL against OFFICIAL_OEM_HOSTS."""
        try:
            return validate_official_download_url(
                brand,
                url,
                resolve_dns=self.resolve_dns,
            )
        except DNSTransientError:
            raise
        except ValueError as exc:
            raise UnofficialDownloadURLError(str(exc)) from exc

    def verify_one(
        self,
        brand: str,
        release: AdapterRelease,
        *,
        failure_count: int = 0,
        raise_on_unofficial: bool = False,
    ) -> ProbeOutcome:
        """Verify a single AdapterRelease via a 64-byte Range probe."""
        if release.delivery_mode != "direct_cdn":
            if (
                release.verification_status in ("VERIFIED_RESOLVER", "VERIFIED_R2_BUNDLE")
                and _RE_FMR_RESOLVER_URL.fullmatch(release.fast_cdn_url)
                and _RE_FMR_RESOLVER_URL.fullmatch(release.original_official_url)
                and bool(release.resolver_meta)
            ):
                return ProbeOutcome(
                    status="VERIFIED",
                    release=release,
                    file_size_bytes=release.file_size_bytes,
                    file_size_str=release.file_size_str,
                    final_url=release.fast_cdn_url,
                    http_status=200,
                )
            return ProbeOutcome(
                status="DEAD_UPSTREAM",
                release=release,
                reason=f"invalid_resolver_release:{release.delivery_mode}:{release.verification_status}",
                retry_after_seconds=DEAD_UPSTREAM_TTL_SECONDS,
            )

        try:
            canonical_fast_url = self.validate_url(brand, release.fast_cdn_url)
            self.validate_url(brand, release.original_official_url)
        except DNSTransientError as exc:
            return ProbeOutcome(
                status="DEFERRED_VERIFY",
                release=release,
                reason=f"dns_transient: {exc}",
                retry_after_seconds=compute_deferred_backoff(failure_count),
                rejected_unofficial=False,
            )
        except UnofficialDownloadURLError as exc:
            if raise_on_unofficial:
                raise
            return ProbeOutcome(
                status="DEAD_UPSTREAM",
                release=release,
                reason=f"unofficial_url: {exc}",
                retry_after_seconds=DEAD_UPSTREAM_TTL_SECONDS,
                rejected_unofficial=True,
            )

        current_url = canonical_fast_url
        try:
            for hop in range(self.max_redirects + 1):
                with self._client.stream(
                    "GET",
                    current_url,
                    headers={
                        "Range": "bytes=0-63",
                        "Accept-Encoding": "identity",
                    },
                    follow_redirects=False,
                ) as resp:
                    status = resp.status_code

                    if status in (301, 302, 303, 307, 308):
                        if hop >= self.max_redirects:
                            return ProbeOutcome(
                                status="DEAD_UPSTREAM",
                                release=release,
                                http_status=status,
                                reason=f"max_redirects_exceeded ({self.max_redirects})",
                                retry_after_seconds=DEAD_UPSTREAM_TTL_SECONDS,
                            )
                        location = resp.headers.get("location")
                        if not location:
                            return ProbeOutcome(
                                status="DEAD_UPSTREAM",
                                release=release,
                                http_status=status,
                                reason="redirect_missing_location",
                                retry_after_seconds=DEAD_UPSTREAM_TTL_SECONDS,
                            )
                        candidate_next = urljoin(current_url, location)
                        try:
                            current_url = self.validate_url(brand, candidate_next)
                        except DNSTransientError as exc:
                            return ProbeOutcome(
                                status="DEFERRED_VERIFY",
                                release=release,
                                http_status=status,
                                reason=f"dns_transient_redirect: {exc}",
                                retry_after_seconds=compute_deferred_backoff(failure_count),
                                rejected_unofficial=False,
                            )
                        except UnofficialDownloadURLError as exc:
                            if raise_on_unofficial:
                                raise
                            return ProbeOutcome(
                                status="DEAD_UPSTREAM",
                                release=release,
                                http_status=status,
                                reason=f"unofficial_redirect: {exc}",
                                retry_after_seconds=DEAD_UPSTREAM_TTL_SECONDS,
                                rejected_unofficial=True,
                            )
                        continue

                    if status == 429 or status == 408 or 500 <= status < 600:
                        backoff = compute_deferred_backoff(failure_count)
                        if status == 429:
                            retry_after = _parse_retry_after_seconds(resp.headers)
                            if retry_after is not None:
                                backoff = max(backoff, retry_after)
                        return ProbeOutcome(
                            status="DEFERRED_VERIFY",
                            release=release,
                            http_status=status,
                            reason=f"transient_http_{status}",
                            retry_after_seconds=backoff,
                        )

                    if status in (404, 410) or status not in (200, 206):
                        return ProbeOutcome(
                            status="DEAD_UPSTREAM",
                            release=release,
                            http_status=status,
                            reason=f"http_{status}",
                            retry_after_seconds=DEAD_UPSTREAM_TTL_SECONDS,
                        )

                    content_type = resp.headers.get("content-type", "").strip().lower()
                    if any(content_type.startswith(p) for p in FORBIDDEN_CONTENT_TYPE_PREFIXES):
                        return ProbeOutcome(
                            status="DEAD_UPSTREAM",
                            release=release,
                            http_status=status,
                            reason=f"soft_404_content_type:{content_type}",
                            retry_after_seconds=DEAD_UPSTREAM_TTL_SECONDS,
                        )

                    # Read strictly at most 64 bytes and immediately close stream!
                    buf = bytearray()
                    for chunk in resp.iter_bytes(chunk_size=64):
                        need = 64 - len(buf)
                        buf.extend(chunk[:need])
                        if len(buf) >= 64:
                            break
                    head_bytes = bytes(buf)

                    expected_magic = _expected_magic_for_package(release.package_type)
                    if expected_magic is not None and not head_bytes.startswith(expected_magic):
                        return ProbeOutcome(
                            status="DEAD_UPSTREAM",
                            release=release,
                            http_status=status,
                            reason=f"bad_magic_for_{release.package_type}",
                            retry_after_seconds=DEAD_UPSTREAM_TTL_SECONDS,
                        )

                    total_size = _extract_total_size(status, resp.headers)
                    if total_size is None:
                        return ProbeOutcome(
                            status="DEAD_UPSTREAM",
                            release=release,
                            http_status=status,
                            reason="missing_total_size_header",
                            retry_after_seconds=DEAD_UPSTREAM_TTL_SECONDS,
                        )

                    min_required = (
                        self.min_incremental_bytes
                        if release.package_type == "incremental_zip"
                        else self.min_full_bytes
                    )
                    if total_size < min_required:
                        return ProbeOutcome(
                            status="DEAD_UPSTREAM",
                            release=release,
                            http_status=status,
                            reason=f"size_below_minimum:{total_size}<{min_required}",
                            retry_after_seconds=DEAD_UPSTREAM_TTL_SECONDS,
                        )

                    size_str = format_file_size(total_size)
                    captured_md5 = (
                        _extract_etag_md5(resp.headers) if not release.md5_or_sha256 else None
                    )
                    verified_release = release.with_probe_result(
                        brand_id=brand,
                        fast_cdn_url=canonical_fast_url,
                        file_size_bytes=total_size,
                        file_size_str=size_str,
                        verification_status="VERIFIED_IMMUTABLE",
                        md5_or_sha256=captured_md5,
                    )
                    return ProbeOutcome(
                        status="VERIFIED",
                        release=verified_release,
                        file_size_bytes=total_size,
                        file_size_str=size_str,
                        final_url=current_url,
                        http_status=status,
                    )

        except UnofficialDownloadURLError:
            raise
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            return ProbeOutcome(
                status="DEFERRED_VERIFY",
                release=release,
                reason=f"transport_error:{type(exc).__name__}",
                retry_after_seconds=compute_deferred_backoff(failure_count),
            )

        return ProbeOutcome(
            status="DEAD_UPSTREAM",
            release=release,
            reason="redirect_loop_exhausted",
            retry_after_seconds=DEAD_UPSTREAM_TTL_SECONDS,
        )

    def verify_batch(
        self,
        brand: str,
        items: Sequence[tuple[AdapterRelease, int]],
    ) -> list[ProbeOutcome]:
        """Concurrently probe `(release, failure_count)` items preserving input order."""
        if not items:
            return []
        if self.verify_workers == 1 or len(items) == 1:
            return [
                self.verify_one(brand, rel, failure_count=failures)
                for rel, failures in items
            ]
        with ThreadPoolExecutor(max_workers=self.verify_workers) as pool:
            futures = [
                pool.submit(self.verify_one, brand, rel, failure_count=failures)
                for rel, failures in items
            ]
            return [fut.result() for fut in futures]


__all__ = [
    "BASE_DEFERRED_BACKOFF_SECONDS",
    "DEAD_UPSTREAM_TTL_SECONDS",
    "DEFAULT_MIN_FULL_BYTES",
    "DEFAULT_MIN_INCREMENTAL_BYTES",
    "MAX_DEFERRED_BACKOFF_SECONDS",
    "ProbeOutcome",
    "RangeProbeVerifier",
    "UnofficialDownloadURLError",
    "VerifyClassification",
    "compute_deferred_backoff",
    "format_file_size",
]
