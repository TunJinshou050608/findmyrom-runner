"""Base interfaces and strict official-only HTTP metadata client for v4 stock adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit
import zlib

import httpx

from fmr_core.publish_spec import (
    DeviceSpec,
    EnrichmentSpec,
    PublishSpecV1,
    ReleaseSpec,
    canonical_version_str,
)
from fmr_ingest_stock.cdn_rewriter import validate_official_download_url

OFFICIAL_METADATA_HOSTS: frozenset[str] = frozenset(
    {
        "developers.google.com",
        "storage.googleapis.com",
        "fota-cloud-dn.ospserver.net",
        "doc.samsungmobile.com",
        "neofussvr.sslcs.cdngc.net",
        "fus.samsungmobile.com",
        "app.swup.update.sony.net",
        "mi.com",
        "www.mi.com",
        "hyperos.mi.com",
        "c.mi.com",
        "update.miui.com",
        "update.intl.miui.com",
        "www.asus.com",
        "odinapi.asus.com",
        "www.vivo.com",
        "component-otapc-cn.allawntech.com",
        "component-otapc-sg.allawnos.com",
        "component-otapc-in.allawnos.com",
        "component-otapc-eu.allawnos.com",
        "component-ota-cn.allawntech.com",
        "component-ota-sg.allawnos.com",
        "component-ota-in.allawnos.com",
        "component-ota-eu.allawnos.com",
    }
)

FORBIDDEN_BINARY_SUFFIXES: tuple[str, ...] = (
    ".zip",
    ".tgz",
    ".gz",
    ".ozip",
    ".enc2",
    ".enc4",
    ".bin",
    ".img",
)

MAX_METADATA_BYTES: int = 24 * 1024 * 1024  # 24 MiB
TRANSIENT_HTTP_STATUSES: frozenset[int] = frozenset({429, 502, 503, 504})
_RE_CTRL_OR_SPACE = re.compile(r"[\s\x00-\x1f\x7f]")
_RE_FMR_RESOLVER_URL = re.compile(
    r"^https://findmyrom\.com/(?:stock/refresh|sony/bundles)/[0-9a-f]{64}$"
)

HTTP_CACHE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS http_cache (
    cache_key TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    status_code INTEGER NOT NULL,
    etag TEXT,
    last_modified TEXT,
    content_type TEXT,
    body_zlib BLOB NOT NULL,
    uncompressed_bytes INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
"""


@dataclass(frozen=True)
class MetadataResponse:
    url: str
    status_code: int
    headers: dict[str, str]
    content: bytes
    from_cache: bool = False

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")


@dataclass(frozen=True)
class AdapterDevice:
    codename: str
    brand_id: str
    marketing_name: str
    model_numbers: tuple[str, ...] = ()
    soc_name: str | None = None
    boot_partition_type: str = "init_boot"
    has_vbmeta: int = 1
    vendor_meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.model_numbers, tuple):
            object.__setattr__(self, "model_numbers", tuple(self.model_numbers))
        if self.vendor_meta is None:
            object.__setattr__(self, "vendor_meta", {})

    def to_device_spec(self) -> DeviceSpec:
        return DeviceSpec(
            codename=self.codename,
            brand_id=self.brand_id,
            marketing_name=self.marketing_name,
            model_numbers=self.model_numbers,
            soc_name=self.soc_name,
            boot_partition_type=self.boot_partition_type,
            has_vbmeta=self.has_vbmeta,
        )


@dataclass(frozen=True)
class AdapterRelease:
    device_codename: str
    model_code: str
    region_code: str
    version_str: str
    android_version: str
    package_type: str
    original_official_url: str
    fast_cdn_url: str
    md5_or_sha256: str | None = None
    published_date: str | None = None
    file_size_bytes: int | None = None
    file_size_str: str | None = None
    delivery_mode: str = "direct_cdn"
    verification_status: str = "VERIFIED_IMMUTABLE"
    resolver_meta: dict[str, Any] | None = None
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

    @property
    def canonical_version(self) -> str:
        return canonical_version_str(self.version_str)

    @property
    def identity_tuple(self) -> tuple[str, str, str, str, str]:
        effective_model = self.model_code.strip() or self.device_codename
        return (
            self.device_codename,
            effective_model,
            self.region_code,
            self.canonical_version,
            self.package_type,
        )

    @property
    def identity_key(self) -> str:
        return json.dumps(self.identity_tuple, separators=(",", ":"))

    @property
    def conflict_signature(self) -> str:
        """Payload signature used to detect 5-tuple collisions across distinct upstream artifacts."""
        payload = {
            "original_official_url": self.original_official_url,
            "fast_cdn_url": self.fast_cdn_url,
            "md5_or_sha256": self.md5_or_sha256 or "",
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    @property
    def input_sha256(self) -> str:
        payload = {
            "device_codename": self.device_codename,
            "model_code": self.model_code,
            "region_code": self.region_code,
            "version_str": self.canonical_version,
            "android_version": self.android_version,
            "package_type": self.package_type,
            "original_official_url": self.original_official_url,
            "fast_cdn_url": self.fast_cdn_url,
            "md5_or_sha256": self.md5_or_sha256,
            "published_date": self.published_date,
            "file_size_bytes": self.file_size_bytes,
            "file_size_str": self.file_size_str,
            "delivery_mode": self.delivery_mode,
            "verification_status": self.verification_status,
            "resolver_meta": self.resolver_meta,
            "display_version": self.display_version,
            "raw_version": self.raw_version,
            "os_name": self.os_name,
            "os_version": self.os_version,
            "security_patch": self.security_patch,
            "original_filename": self.original_filename,
            "vendor_fields": self.vendor_fields,
            "sources": self.sources,
            "availability": self.availability,
            "unavailable_reason": self.unavailable_reason,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def with_probe_result(
        self,
        *,
        brand_id: str,
        fast_cdn_url: str | None = None,
        file_size_bytes: int | None = None,
        file_size_str: str | None = None,
        verification_status: str | None = None,
        md5_or_sha256: str | None = None,
    ) -> AdapterRelease:
        resolved_fast = fast_cdn_url or self.fast_cdn_url
        if self.delivery_mode != "direct_cdn":
            if not _RE_FMR_RESOLVER_URL.fullmatch(resolved_fast):
                raise ValueError(
                    f"Invalid resolver/bundle fast_cdn_url for delivery_mode={self.delivery_mode!r}: {resolved_fast!r}"
                )
            if not _RE_FMR_RESOLVER_URL.fullmatch(self.original_official_url):
                raise ValueError(
                    f"Invalid resolver/bundle original_official_url for delivery_mode={self.delivery_mode!r}: {self.original_official_url!r}"
                )
            validated_fast = resolved_fast
            validated_orig = self.original_official_url
        else:
            validated_fast = validate_official_download_url(
                brand_id, resolved_fast, resolve_dns=False
            )
            validated_orig = validate_official_download_url(
                brand_id, self.original_official_url, resolve_dns=False
            )
        return replace(
            self,
            version_str=self.canonical_version,
            original_official_url=validated_orig,
            fast_cdn_url=validated_fast,
            md5_or_sha256=md5_or_sha256 if md5_or_sha256 is not None else self.md5_or_sha256,
            file_size_bytes=file_size_bytes if file_size_bytes is not None else self.file_size_bytes,
            file_size_str=file_size_str if file_size_str is not None else self.file_size_str,
            verification_status=verification_status or self.verification_status,
        )

    def to_publish_spec(self, device: AdapterDevice) -> PublishSpecV1:
        device_spec = device.to_device_spec()
        release_spec = ReleaseSpec(
            device_codename=self.device_codename,
            model_code=self.model_code,
            region_code=self.region_code,
            version_str=self.canonical_version,
            android_version=self.android_version,
            package_type=self.package_type,
            original_official_url=self.original_official_url,
            fast_cdn_url=self.fast_cdn_url,
            file_size_bytes=self.file_size_bytes,
            file_size_str=self.file_size_str,
            md5_or_sha256=self.md5_or_sha256,
            published_date=self.published_date,
            delivery_mode=self.delivery_mode,
            verification_status=self.verification_status,
            resolver_meta=self.resolver_meta,
        )
        enrichment_spec = EnrichmentSpec(
            display_version=self.display_version or self.version_str,
            raw_version=self.raw_version or self.version_str,
            os_name=self.os_name,
            os_version=self.os_version,
            security_patch=self.security_patch,
            original_filename=self.original_filename,
            vendor_fields=self.vendor_fields,
            sources=self.sources,
            availability=self.availability or "available",
            unavailable_reason=self.unavailable_reason,
        )
        return PublishSpecV1(
            device=device_spec,
            release=release_spec,
            enrichment=enrichment_spec,
        )


def validate_official_metadata_url(
    url: str,
    *,
    allowed_hosts: frozenset[str] | set[str] | tuple[str, ...] = OFFICIAL_METADATA_HOSTS,
) -> str:
    """Validate that a metadata request URL targets an allowed official host and is not a binary ROM."""
    if not isinstance(url, str):
        raise ValueError("Metadata URL must be a string")
    cleaned = url.strip()
    if not cleaned or _RE_CTRL_OR_SPACE.search(cleaned):
        raise ValueError(f"Metadata URL contains whitespace or control characters: {url!r}")
    parsed = urlsplit(cleaned)
    if parsed.scheme.lower() != "https":
        raise ValueError(f"Metadata URL must use HTTPS: {url!r}")
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise ValueError(f"Metadata URL has invalid authority: {url!r}")
    if parsed.port not in (None, 443):
        raise ValueError(f"Metadata URL uses disallowed port: {url!r}")

    host_lower = parsed.hostname.lower()
    allowed_lower = {h.strip().lower() for h in allowed_hosts if h and h.strip()}
    if host_lower not in allowed_lower:
        raise ValueError(f"Host {host_lower!r} is not in OFFICIAL_METADATA_HOSTS allowlist")

    path_lower = (parsed.path or "").lower()
    if any(path_lower.endswith(suffix) for suffix in FORBIDDEN_BINARY_SUFFIXES):
        raise ValueError(f"Binary ROM suffix is forbidden in metadata client: {url!r}")

    return urlunsplit(("https", host_lower, parsed.path, parsed.query, ""))


class OfficialMetadataHTTP:
    """Official-only metadata HTTP wrapper with 24 MiB cap, binary suffix block, and SQLite 304 cache."""

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        cache_conn: sqlite3.Connection | None = None,
        cache_db_path: str | Path | None = None,
        allowed_hosts: frozenset[str] | set[str] | tuple[str, ...] = OFFICIAL_METADATA_HOSTS,
        max_bytes: int = MAX_METADATA_BYTES,
        max_retries: int = 2,
        max_redirects: int = 3,
        timeout: float = 30.0,
        sleep_fn: Callable[[float], None] = time.sleep,
        now_fn: Callable[[], int] | None = None,
    ) -> None:
        if max_bytes <= 0 or max_bytes > MAX_METADATA_BYTES:
            raise ValueError(f"max_bytes must be in 1..{MAX_METADATA_BYTES}, got {max_bytes}")
        self.allowed_hosts = frozenset(h.strip().lower() for h in allowed_hosts if h and h.strip())
        self.max_bytes = max_bytes
        self.max_retries = max(0, int(max_retries))
        self.max_redirects = max(0, int(max_redirects))
        self._sleep_fn = sleep_fn
        self._now_fn = now_fn or (lambda: int(time.time()))

        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(timeout),
            follow_redirects=False,
            headers={
                "User-Agent": "FindMyROM-OfficialIngest/4.0 (+https://findmyrom.com)",
                "Accept-Encoding": "gzip, deflate",
            },
        )

        self._owns_cache_conn = False
        self._cache_conn: sqlite3.Connection | None = cache_conn
        if self._cache_conn is None and cache_db_path is not None:
            db_path = Path(cache_db_path)
            db_path.parent.mkdir(parents=True, exist_ok=True)
            self._cache_conn = sqlite3.connect(str(db_path))
            self._cache_conn.row_factory = sqlite3.Row
            self._cache_conn.execute("PRAGMA journal_mode = WAL;")
            self._owns_cache_conn = True

        if self._cache_conn is not None:
            self._cache_conn.executescript(HTTP_CACHE_SCHEMA_SQL)
            self._cache_conn.commit()

    @property
    def client(self) -> httpx.Client:
        return self._client

    def close(self) -> None:
        if self._owns_client:
            self._client.close()
        if self._owns_cache_conn and self._cache_conn is not None:
            self._cache_conn.close()
            self._cache_conn = None

    def __enter__(self) -> OfficialMetadataHTTP:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    @staticmethod
    def _build_cache_key(url: str, cookies: Mapping[str, str] | None) -> str:
        cookie_part = ""
        if cookies:
            cookie_part = "&".join(f"{k}={cookies[k]}" for k, v in sorted(cookies.items()))
        raw = f"GET|{url}|{cookie_part}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def invalidate(self, url: str, *, cookies: Mapping[str, str] | None = None) -> None:
        """Evict a URL+cookie entry from the SQLite HTTP cache so invalid/TOS-wall bodies never persist."""
        if self._cache_conn is None:
            return
        validated_url = validate_official_metadata_url(url, allowed_hosts=self.allowed_hosts)
        cache_key = self._build_cache_key(validated_url, cookies)
        self._cache_conn.execute("DELETE FROM http_cache WHERE cache_key = ?", (cache_key,))
        self._cache_conn.commit()

    def _load_cached(self, cache_key: str) -> dict[str, Any] | None:
        if self._cache_conn is None:
            return None
        cur = self._cache_conn.execute(
            "SELECT cache_key, url, status_code, etag, last_modified, content_type, body_zlib, uncompressed_bytes "
            "FROM http_cache WHERE cache_key = ?",
            (cache_key,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        if isinstance(row, sqlite3.Row):
            return dict(row)
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))

    def _store_cached(
        self,
        *,
        cache_key: str,
        url: str,
        status_code: int,
        etag: str | None,
        last_modified: str | None,
        content_type: str | None,
        body: bytes,
    ) -> None:
        if self._cache_conn is None or len(body) > self.max_bytes:
            return
        compressed = zlib.compress(body, level=6)
        now_ts = self._now_fn()
        self._cache_conn.execute(
            """
            INSERT INTO http_cache (
                cache_key, url, status_code, etag, last_modified, content_type,
                body_zlib, uncompressed_bytes, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
                url = excluded.url,
                status_code = excluded.status_code,
                etag = excluded.etag,
                last_modified = excluded.last_modified,
                content_type = excluded.content_type,
                body_zlib = excluded.body_zlib,
                uncompressed_bytes = excluded.uncompressed_bytes,
                updated_at = excluded.updated_at
            """,
            (
                cache_key,
                url,
                status_code,
                etag,
                last_modified,
                content_type,
                compressed,
                len(body),
                now_ts,
            ),
        )
        self._cache_conn.commit()

    def _touch_cached_304(
        self,
        *,
        cache_key: str,
        etag: str | None,
        last_modified: str | None,
    ) -> None:
        if self._cache_conn is None:
            return
        now_ts = self._now_fn()
        self._cache_conn.execute(
            """
            UPDATE http_cache
            SET etag = COALESCE(?, etag),
                last_modified = COALESCE(?, last_modified),
                updated_at = ?
            WHERE cache_key = ?
            """,
            (etag, last_modified, now_ts, cache_key),
        )
        self._cache_conn.commit()

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        cookies: Mapping[str, str] | None = None,
        use_cache: bool = True,
    ) -> MetadataResponse:
        validated_url = validate_official_metadata_url(url, allowed_hosts=self.allowed_hosts)
        cache_key = self._build_cache_key(validated_url, cookies)
        cached = self._load_cached(cache_key) if use_cache else None

        req_headers: dict[str, str] = {}
        if headers:
            req_headers.update(headers)
        if cookies:
            cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())
            if "Cookie" in req_headers:
                req_headers["Cookie"] = f"{req_headers['Cookie']}; {cookie_header}"
            else:
                req_headers["Cookie"] = cookie_header

        if cached is not None:
            if cached.get("etag"):
                req_headers.setdefault("If-None-Match", str(cached["etag"]))
            if cached.get("last_modified"):
                req_headers.setdefault("If-Modified-Since", str(cached["last_modified"]))

        attempt = 0
        last_exc: Exception | None = None
        while attempt <= self.max_retries:
            try:
                return self._execute_single_get(
                    validated_url,
                    req_headers=req_headers,
                    cache_key=cache_key,
                    cached=cached,
                    use_cache=use_cache,
                )
            except (httpx.TransportError, _TransientMetadataHTTPError) as exc:
                last_exc = exc
                if attempt >= self.max_retries:
                    break
                delay = min(4.0, 0.5 * (2**attempt))
                self._sleep_fn(delay)
                attempt += 1

        assert last_exc is not None
        if isinstance(last_exc, _TransientMetadataHTTPError):
            raise httpx.HTTPStatusError(
                f"Transient metadata HTTP {last_exc.status_code} for {validated_url}",
                request=last_exc.request,
                response=last_exc.response,
            ) from last_exc
        raise last_exc

    def _execute_single_get(
        self,
        initial_url: str,
        *,
        req_headers: dict[str, str],
        cache_key: str,
        cached: dict[str, Any] | None,
        use_cache: bool,
    ) -> MetadataResponse:
        current_url = initial_url
        current_headers = dict(req_headers)

        for hop in range(self.max_redirects + 1):
            with self._client.stream(
                "GET",
                current_url,
                headers=current_headers,
                follow_redirects=False,
            ) as resp:
                status = resp.status_code
                if status in (301, 302, 303, 307, 308):
                    if hop >= self.max_redirects:
                        raise ValueError(f"Exceeded max metadata redirects ({self.max_redirects}) for {initial_url}")
                    location = resp.headers.get("location")
                    if not location:
                        raise ValueError(f"Redirect response {status} missing Location header for {current_url}")
                    next_raw = urljoin(current_url, location)
                    next_url = validate_official_metadata_url(next_raw, allowed_hosts=self.allowed_hosts)
                    if urlsplit(next_url).hostname != urlsplit(current_url).hostname:
                        current_headers.pop("Cookie", None)
                    current_url = next_url
                    continue

                if status == 304 and cached is not None:
                    new_etag = resp.headers.get("etag")
                    new_lm = resp.headers.get("last-modified")
                    self._touch_cached_304(
                        cache_key=cache_key,
                        etag=new_etag,
                        last_modified=new_lm,
                    )
                    raw_body = zlib.decompress(cached["body_zlib"])
                    if len(raw_body) > self.max_bytes:
                        raise ValueError(f"Cached response exceeds max_bytes ({self.max_bytes})")
                    merged_headers = {k.lower(): v for k, v in resp.headers.items()}
                    if cached.get("content_type") and "content-type" not in merged_headers:
                        merged_headers["content-type"] = str(cached["content_type"])
                    return MetadataResponse(
                        url=current_url,
                        status_code=200,
                        headers=merged_headers,
                        content=raw_body,
                        from_cache=True,
                    )

                if status in TRANSIENT_HTTP_STATUSES:
                    raise _TransientMetadataHTTPError(status, resp.request, resp)

                if status >= 400:
                    resp.raise_for_status()

                cl_header = resp.headers.get("content-length")
                if cl_header is not None:
                    try:
                        cl_val = int(cl_header.strip())
                    except ValueError:
                        cl_val = None
                    if cl_val is not None and cl_val > self.max_bytes:
                        raise ValueError(
                            f"Metadata Content-Length {cl_val} exceeds cap {self.max_bytes} for {current_url}"
                        )

                chunks: list[bytes] = []
                total = 0
                for chunk in resp.iter_bytes(chunk_size=65536):
                    total += len(chunk)
                    if total > self.max_bytes:
                        raise ValueError(
                            f"Metadata body exceeded {self.max_bytes} bytes for {current_url}"
                        )
                    chunks.append(chunk)

                body = b"".join(chunks)
                resp_headers = {k.lower(): v for k, v in resp.headers.items()}
                if use_cache and status == 200:
                    self._store_cached(
                        cache_key=cache_key,
                        url=initial_url,
                        status_code=200,
                        etag=resp_headers.get("etag"),
                        last_modified=resp_headers.get("last-modified"),
                        content_type=resp_headers.get("content-type"),
                        body=body,
                    )
                return MetadataResponse(
                    url=current_url,
                    status_code=status,
                    headers=resp_headers,
                    content=body,
                    from_cache=False,
                )

        raise ValueError(f"Redirect loop failed for {initial_url}")

    def post(
        self,
        url: str,
        *,
        content: bytes | str | None = None,
        data: Mapping[str, str] | None = None,
        json_body: Any = None,
        headers: Mapping[str, str] | None = None,
        cookies: Mapping[str, str] | None = None,
    ) -> MetadataResponse:
        validated_url = validate_official_metadata_url(url, allowed_hosts=self.allowed_hosts)

        req_headers: dict[str, str] = {}
        if headers:
            req_headers.update(headers)
        if cookies:
            cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())
            if "Cookie" in req_headers:
                req_headers["Cookie"] = f"{req_headers['Cookie']}; {cookie_header}"
            else:
                req_headers["Cookie"] = cookie_header

        attempt = 0
        last_exc: Exception | None = None
        while attempt <= self.max_retries:
            try:
                return self._execute_single_post(
                    validated_url,
                    req_headers=req_headers,
                    content=content,
                    data=data,
                    json_body=json_body,
                )
            except (httpx.TransportError, _TransientMetadataHTTPError) as exc:
                last_exc = exc
                if attempt >= self.max_retries:
                    break
                delay = min(4.0, 0.5 * (2**attempt))
                self._sleep_fn(delay)
                attempt += 1

        assert last_exc is not None
        if isinstance(last_exc, _TransientMetadataHTTPError):
            raise httpx.HTTPStatusError(
                f"Transient metadata HTTP {last_exc.status_code} for {validated_url}",
                request=last_exc.request,
                response=last_exc.response,
            ) from last_exc
        raise last_exc

    def _execute_single_post(
        self,
        initial_url: str,
        *,
        req_headers: dict[str, str],
        content: bytes | str | None,
        data: Mapping[str, str] | None,
        json_body: Any,
    ) -> MetadataResponse:
        current_url = initial_url
        current_headers = dict(req_headers)
        method = "POST"
        cur_content = content
        cur_data = data
        cur_json = json_body

        for hop in range(self.max_redirects + 1):
            stream_kwargs: dict[str, Any] = {
                "headers": current_headers,
                "follow_redirects": False,
            }
            if method == "POST":
                if cur_content is not None:
                    stream_kwargs["content"] = cur_content
                elif cur_data is not None:
                    stream_kwargs["data"] = cur_data
                elif cur_json is not None:
                    stream_kwargs["json"] = cur_json

            with self._client.stream(method, current_url, **stream_kwargs) as resp:
                status = resp.status_code
                if status in (301, 302, 303, 307, 308):
                    if hop >= self.max_redirects:
                        raise ValueError(
                            f"Exceeded max metadata redirects ({self.max_redirects}) for {initial_url}"
                        )
                    location = resp.headers.get("location")
                    if not location:
                        raise ValueError(
                            f"Redirect response {status} missing Location header for {current_url}"
                        )
                    next_raw = urljoin(current_url, location)
                    next_url = validate_official_metadata_url(
                        next_raw, allowed_hosts=self.allowed_hosts
                    )
                    if urlsplit(next_url).hostname != urlsplit(current_url).hostname:
                        current_headers.pop("Cookie", None)
                    if status in (301, 302, 303):
                        method = "GET"
                        cur_content = None
                        cur_data = None
                        cur_json = None
                    current_url = next_url
                    continue

                if status in TRANSIENT_HTTP_STATUSES:
                    raise _TransientMetadataHTTPError(status, resp.request, resp)

                if status >= 400:
                    resp.raise_for_status()

                cl_header = resp.headers.get("content-length")
                if cl_header is not None:
                    try:
                        cl_val = int(cl_header.strip())
                    except ValueError:
                        cl_val = None
                    if cl_val is not None and cl_val > self.max_bytes:
                        raise ValueError(
                            f"Metadata Content-Length {cl_val} exceeds cap {self.max_bytes} for {current_url}"
                        )

                chunks: list[bytes] = []
                total = 0
                for chunk in resp.iter_bytes(chunk_size=65536):
                    total += len(chunk)
                    if total > self.max_bytes:
                        raise ValueError(
                            f"Metadata body exceeded {self.max_bytes} bytes for {current_url}"
                        )
                    chunks.append(chunk)

                body = b"".join(chunks)
                resp_headers = {k.lower(): v for k, v in resp.headers.items()}
                return MetadataResponse(
                    url=current_url,
                    status_code=status,
                    headers=resp_headers,
                    content=body,
                    from_cache=False,
                )

        raise ValueError(f"Redirect loop failed for {initial_url}")


class _TransientMetadataHTTPError(Exception):
    def __init__(self, status_code: int, request: httpx.Request, response: httpx.Response) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code
        self.request = request
        self.response = response


class VendorAdapter(ABC):
    """Abstract contract implemented by every official OEM ingestion station."""

    vendor_id: str

    @abstractmethod
    def discover_devices(self, http: OfficialMetadataHTTP) -> list[AdapterDevice]:
        """Discover supported devices from official vendor metadata."""

    @abstractmethod
    def fetch_releases(
        self,
        device: AdapterDevice,
        http: OfficialMetadataHTTP,
    ) -> list[AdapterRelease]:
        """Fetch official firmware releases for a single discovered device."""

    def resolve_delivery(
        self,
        release: AdapterRelease,
        http: OfficialMetadataHTTP,
    ) -> AdapterRelease:
        """Optionally resolve dynamic delivery parameters before Range verification."""
        return release


__all__ = [
    "AdapterDevice",
    "AdapterRelease",
    "FORBIDDEN_BINARY_SUFFIXES",
    "HTTP_CACHE_SCHEMA_SQL",
    "MAX_METADATA_BYTES",
    "MetadataResponse",
    "OFFICIAL_METADATA_HOSTS",
    "OfficialMetadataHTTP",
    "VendorAdapter",
    "validate_official_metadata_url",
]
