"""Per-vendor isolated Collector (VendorAdapter -> RangeProbeVerifier -> OutboxSpool)."""

from __future__ import annotations

from collections.abc import Callable
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from typing import Any

import httpx

from fmr_core.publish_spec import PublishSpecV1
from fmr_ingest_stock.adapters import (
    AdapterDevice,
    AdapterRelease,
    OfficialMetadataHTTP,
    VendorAdapter,
    get_adapter,
    load_vendor_configs,
)
from fmr_ingest_stock.adapters.base import HTTP_CACHE_SCHEMA_SQL

from .locks import LockBusyError, acquire_file_lock
from .outbox import DEFAULT_OUTBOX_BATCH_SIZE, OutboxSpool
from .verifier import (
    DEAD_UPSTREAM_TTL_SECONDS,
    DEFAULT_MIN_FULL_BYTES,
    RangeProbeVerifier,
    compute_deferred_backoff,
)

COLLECTOR_SCHEMA_SQL = (
    HTTP_CACHE_SCHEMA_SQL
    + """
CREATE TABLE IF NOT EXISTS collector_devices (
    codename TEXT PRIMARY KEY,
    brand_id TEXT NOT NULL,
    marketing_name TEXT NOT NULL,
    model_numbers_json TEXT NOT NULL,
    boot_partition_type TEXT NOT NULL,
    device_sha256 TEXT NOT NULL,
    updated_at INTEGER NOT NULL,
    last_checked_at INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS collector_releases (
    identity_key TEXT PRIMARY KEY,
    device_codename TEXT NOT NULL,
    model_code TEXT NOT NULL,
    region_code TEXT NOT NULL,
    version_str TEXT NOT NULL,
    package_type TEXT NOT NULL,
    conflict_sig TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    verify_state TEXT NOT NULL,
    file_size_bytes INTEGER,
    file_size_str TEXT,
    verified_fast_url TEXT,
    verified_checksum TEXT,
    failure_count INTEGER NOT NULL DEFAULT 0,
    next_retry_at INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    last_spooled_hash TEXT,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS quarantined_collisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    identity_key TEXT NOT NULL,
    existing_payload_json TEXT NOT NULL,
    conflicting_payload_json TEXT NOT NULL,
    detected_at INTEGER NOT NULL
);
"""
)


def _device_fingerprint(device: AdapterDevice) -> str:
    payload = device.to_device_spec().to_dict()
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _open_collector_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA busy_timeout = 5000;")
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.executescript(COLLECTOR_SCHEMA_SQL)
    existing_cols = {
        str(r["name"]) for r in conn.execute("PRAGMA table_info(collector_devices)").fetchall()
    }
    if "last_checked_at" not in existing_cols:
        conn.execute(
            "ALTER TABLE collector_devices ADD COLUMN last_checked_at INTEGER NOT NULL DEFAULT 0"
        )
    rel_cols = {
        str(r["name"]) for r in conn.execute("PRAGMA table_info(collector_releases)").fetchall()
    }
    if "verified_checksum" not in rel_cols:
        conn.execute("ALTER TABLE collector_releases ADD COLUMN verified_checksum TEXT")
    conn.commit()
    return conn


def _open_http_cache_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA busy_timeout = 5000;")
    return conn


def run_collector(
    vendor_id: str,
    *,
    state_dir: str | Path,
    adapter: VendorAdapter | None = None,
    vendors_yaml_path: str | Path | None = None,
    metadata_client: httpx.Client | None = None,
    range_client: httpx.Client | None = None,
    verify_workers: int | None = None,
    max_devices: int | None = None,
    resolve_dns: bool = True,
    min_full_bytes: int = DEFAULT_MIN_FULL_BYTES,
    outbox_batch_size: int = DEFAULT_OUTBOX_BATCH_SIZE,
    max_collect_seconds: int | None = None,
    allow_disabled: bool = False,
    now_fn: Callable[[], int] | None = None,
) -> dict[str, Any]:
    """Run an isolated per-vendor collection & Range verification pass into Outbox."""
    vendor_key = vendor_id.strip().lower()
    root = Path(state_dir)
    clock = now_fn or (lambda: int(time.time()))

    configs = load_vendor_configs(vendors_yaml_path)
    cfg = configs.get(vendor_key)
    if cfg is not None and not cfg.enabled and not allow_disabled and adapter is None:
        return {
            "vendor": vendor_key,
            "status": f"disabled:{cfg.status}",
            "devices_discovered": 0,
            "devices_checked": 0,
            "device_fetch_errors": 0,
            "releases_seen": 0,
            "spooled_specs": 0,
        }

    effective_workers = (
        verify_workers
        if verify_workers is not None
        else (cfg.verify_workers if cfg is not None else 4)
    )
    effective_deadline_sec = (
        max_collect_seconds
        if max_collect_seconds is not None
        else (cfg.max_collect_seconds if cfg is not None else 600)
    )
    effective_max_devices = (
        max_devices
        if max_devices is not None
        else (cfg.max_devices if cfg is not None else None)
    )
    if effective_max_devices is not None and int(effective_max_devices) <= 0:
        raise ValueError(f"max_devices must be positive, got {effective_max_devices}")

    lock_path = root / "locks" / f"collect-{vendor_key}.lock"
    try:
        with acquire_file_lock(lock_path):
            return _execute_collector_locked(
                vendor_key=vendor_key,
                root=root,
                adapter=adapter or get_adapter(vendor_key),
                metadata_client=metadata_client,
                range_client=range_client,
                verify_workers=effective_workers,
                max_devices=int(effective_max_devices) if effective_max_devices is not None else None,
                resolve_dns=resolve_dns,
                min_full_bytes=min_full_bytes,
                outbox_batch_size=outbox_batch_size,
                max_collect_seconds=effective_deadline_sec,
                clock=clock,
            )
    except LockBusyError:
        return {
            "vendor": vendor_key,
            "status": "skipped_locked",
            "lock_path": str(lock_path),
            "devices_discovered": 0,
            "devices_checked": 0,
            "device_fetch_errors": 0,
            "releases_seen": 0,
            "spooled_specs": 0,
        }


def _execute_collector_locked(
    *,
    vendor_key: str,
    root: Path,
    adapter: VendorAdapter,
    metadata_client: httpx.Client | None,
    range_client: httpx.Client | None,
    verify_workers: int,
    max_devices: int | None,
    resolve_dns: bool,
    min_full_bytes: int,
    outbox_batch_size: int,
    max_collect_seconds: int,
    clock: Callable[[], int],
) -> dict[str, Any]:
    started_monotonic = time.monotonic()
    deadline_monotonic = started_monotonic + max(1, int(max_collect_seconds))

    db_path = root / "collectors" / f"{vendor_key}.sqlite"
    conn = _open_collector_db(db_path)
    http_cache_conn = _open_http_cache_db(db_path)
    outbox = OutboxSpool(root)

    try:
        with (
            OfficialMetadataHTTP(
                client=metadata_client,
                cache_conn=http_cache_conn,
                now_fn=clock,
                sleep_fn=lambda _: None if metadata_client is not None else time.sleep(_),
            ) as http,
            RangeProbeVerifier(
                client=range_client,
                verify_workers=verify_workers,
                resolve_dns=resolve_dns,
                min_full_bytes=min_full_bytes,
            ) as verifier,
        ):
            devices = adapter.discover_devices(http)
            prior_last_checked_at: dict[str, int] = {
                str(r["codename"]): int(r["last_checked_at"] or 0)
                for r in conn.execute(
                    "SELECT codename, last_checked_at FROM collector_devices"
                ).fetchall()
            }
            now_ts = clock()

            for dev in devices:
                conn.execute(
                    """
                    INSERT INTO collector_devices (
                        codename, brand_id, marketing_name, model_numbers_json,
                        boot_partition_type, device_sha256, updated_at, last_checked_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(codename) DO UPDATE SET
                        brand_id = excluded.brand_id,
                        marketing_name = excluded.marketing_name,
                        model_numbers_json = excluded.model_numbers_json,
                        boot_partition_type = excluded.boot_partition_type,
                        device_sha256 = excluded.device_sha256,
                        updated_at = excluded.updated_at
                    """,
                    (
                        dev.codename,
                        dev.brand_id,
                        dev.marketing_name,
                        json.dumps(list(dev.model_numbers)),
                        dev.boot_partition_type,
                        _device_fingerprint(dev),
                        now_ts,
                        prior_last_checked_at.get(dev.codename, 0),
                    ),
                )
            conn.commit()

            devices = sorted(
                devices,
                key=lambda d: (prior_last_checked_at.get(d.codename, 0), d.codename),
            )

            existing_rows: dict[str, dict[str, Any]] = {
                str(r["identity_key"]): dict(r)
                for r in conn.execute("SELECT * FROM collector_releases").fetchall()
            }

            seen_candidates: dict[str, tuple[AdapterDevice, AdapterRelease]] = {}
            collided_keys: set[str] = set()
            collisions_quarantined = 0
            total_raw_releases = 0
            devices_checked = 0
            device_fetch_errors = 0
            probed_verified = 0
            probed_dead = 0
            probed_deferred = 0
            skipped_cached_probe = 0
            skipped_backoff = 0
            unchanged_not_spooled = 0
            total_spooled_specs = 0
            total_outbox_files_created = 0

            pending_outbox_specs: list[PublishSpecV1] = []
            pending_spool_hashes: list[tuple[str, int, str]] = []
            pending_checked_codenames: list[tuple[int, str]] = []

            def _evict_buffered_collision(ikey: str) -> None:
                nonlocal pending_outbox_specs, pending_spool_hashes
                if not pending_outbox_specs:
                    return
                pending_outbox_specs = [
                    s for s in pending_outbox_specs if s.identity_key != ikey
                ]
                pending_spool_hashes = [
                    item for item in pending_spool_hashes if item[2] != ikey
                ]

            def _flush_outbox_buffer() -> None:
                nonlocal total_spooled_specs, total_outbox_files_created
                if pending_outbox_specs:
                    created = outbox.enqueue_specs(
                        vendor_key,
                        pending_outbox_specs,
                        batch_size=outbox_batch_size,
                    )
                    total_spooled_specs += len(pending_outbox_specs)
                    total_outbox_files_created += len(created)
                    conn.executemany(
                        "UPDATE collector_releases SET last_spooled_hash = ?, updated_at = ? WHERE identity_key = ?",
                        pending_spool_hashes,
                    )
                    for combined_hash, upd_ts, ikey in pending_spool_hashes:
                        if ikey in existing_rows:
                            existing_rows[ikey]["last_spooled_hash"] = combined_hash
                            existing_rows[ikey]["updated_at"] = upd_ts
                    pending_outbox_specs.clear()
                    pending_spool_hashes.clear()

                if pending_checked_codenames:
                    conn.executemany(
                        "UPDATE collector_devices SET last_checked_at = ? WHERE codename = ?",
                        pending_checked_codenames,
                    )
                    pending_checked_codenames.clear()

                conn.commit()

            probe_chunk_size = max(1, verify_workers * 4)

            for dev in devices:
                if devices_checked > 0 and (
                    (max_devices is not None and devices_checked >= max_devices)
                    or time.monotonic() >= deadline_monotonic
                ):
                    break

                devices_checked += 1
                try:
                    dev_releases = adapter.fetch_releases(dev, http)
                except httpx.HTTPError:
                    device_fetch_errors += 1
                    if conn.in_transaction:
                        conn.commit()
                    continue
                dev_candidates: list[AdapterRelease] = []

                for rel in dev_releases:
                    total_raw_releases += 1
                    ikey = rel.identity_key
                    if ikey in collided_keys:
                        conn.execute(
                            """
                            INSERT INTO quarantined_collisions (
                                identity_key, existing_payload_json, conflicting_payload_json, detected_at
                            ) VALUES (?, ?, ?, ?)
                            """,
                            (
                                ikey,
                                json.dumps({"quarantined": True}),
                                json.dumps(
                                    {
                                        "url": rel.original_official_url,
                                        "checksum": rel.md5_or_sha256,
                                    },
                                    sort_keys=True,
                                ),
                                clock(),
                            ),
                        )
                        collisions_quarantined += 1
                        continue

                    if ikey in seen_candidates:
                        _, prev_rel = seen_candidates[ikey]
                        if prev_rel.conflict_signature == rel.conflict_signature:
                            continue
                        # Genuine 5-tuple collision across distinct artifacts: quarantine!
                        collided_keys.add(ikey)
                        seen_candidates.pop(ikey, None)
                        _evict_buffered_collision(ikey)
                        conn.execute(
                            """
                            INSERT INTO quarantined_collisions (
                                identity_key, existing_payload_json, conflicting_payload_json, detected_at
                            ) VALUES (?, ?, ?, ?)
                            """,
                            (
                                ikey,
                                json.dumps(
                                    {
                                        "url": prev_rel.original_official_url,
                                        "fast_cdn_url": prev_rel.fast_cdn_url,
                                        "checksum": prev_rel.md5_or_sha256,
                                    },
                                    sort_keys=True,
                                ),
                                json.dumps(
                                    {
                                        "url": rel.original_official_url,
                                        "fast_cdn_url": rel.fast_cdn_url,
                                        "checksum": rel.md5_or_sha256,
                                    },
                                    sort_keys=True,
                                ),
                                clock(),
                            ),
                        )
                        collisions_quarantined += 1
                        continue

                    seen_candidates[ikey] = (dev, rel)
                    dev_candidates.append(rel)

                if conn.in_transaction:
                    conn.commit()

                dev_verified_ready: list[tuple[AdapterRelease, str | None]] = []
                dev_due_for_probe: list[tuple[AdapterRelease, int, str | None]] = []

                now_ts = clock()
                for rel in dev_candidates:
                    ikey = rel.identity_key
                    stored = existing_rows.get(ikey)
                    last_spooled = (
                        str(stored["last_spooled_hash"])
                        if stored and stored["last_spooled_hash"]
                        else None
                    )

                    if stored is not None and stored["input_sha256"] == rel.input_sha256:
                        state = stored["verify_state"]
                        if state == "VERIFIED" and (
                            rel.delivery_mode != "direct_cdn"
                            or stored["file_size_str"] is not None
                        ):
                            hydrated = rel.with_probe_result(
                                brand_id=dev.brand_id,
                                fast_cdn_url=stored["verified_fast_url"] or rel.fast_cdn_url,
                                file_size_bytes=stored["file_size_bytes"],
                                file_size_str=stored["file_size_str"],
                                md5_or_sha256=stored.get("verified_checksum")
                                or rel.md5_or_sha256,
                            )
                            dev_verified_ready.append((hydrated, last_spooled))
                            skipped_cached_probe += 1
                            continue

                        if state in ("DEAD_UPSTREAM", "DEFERRED_VERIFY") and now_ts < int(
                            stored["next_retry_at"] or 0
                        ):
                            skipped_backoff += 1
                            continue

                    prev_failures = int(stored["failure_count"] or 0) if stored is not None else 0
                    dev_due_for_probe.append((rel, prev_failures, last_spooled))

                device_truncated = False
                for offset in range(0, len(dev_due_for_probe), probe_chunk_size):
                    if offset > 0 and time.monotonic() >= deadline_monotonic:
                        device_truncated = True
                        break
                    chunk = dev_due_for_probe[offset : offset + probe_chunk_size]
                    resolved_items: list[tuple[AdapterRelease, int, str | None]] = []
                    for rel, prev_failures, last_spooled in chunk:
                        resolved_rel = adapter.resolve_delivery(rel, http)
                        resolved_items.append((resolved_rel, prev_failures, last_spooled))

                    outcomes = verifier.verify_batch(
                        dev.brand_id,
                        [(rel_item, failures) for rel_item, failures, _ in resolved_items],
                    )
                    now_ts = clock()
                    for (orig_rel, prev_failures, last_spooled), outcome in zip(
                        resolved_items, outcomes
                    ):
                        ikey = orig_rel.identity_key
                        if outcome.status == "VERIFIED":
                            probed_verified += 1
                            vrel = outcome.release
                            conn.execute(
                                """
                                INSERT INTO collector_releases (
                                    identity_key, device_codename, model_code, region_code, version_str,
                                    package_type, conflict_sig, input_sha256, verify_state,
                                    file_size_bytes, file_size_str, verified_fast_url, verified_checksum,
                                    failure_count, next_retry_at, last_error, last_spooled_hash, updated_at
                                )
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'VERIFIED', ?, ?, ?, ?, 0, 0, NULL, ?, ?)
                                ON CONFLICT(identity_key) DO UPDATE SET
                                    conflict_sig = excluded.conflict_sig,
                                    input_sha256 = excluded.input_sha256,
                                    verify_state = 'VERIFIED',
                                    file_size_bytes = excluded.file_size_bytes,
                                    file_size_str = excluded.file_size_str,
                                    verified_fast_url = excluded.verified_fast_url,
                                    verified_checksum = excluded.verified_checksum,
                                    failure_count = 0,
                                    next_retry_at = 0,
                                    last_error = NULL,
                                    updated_at = excluded.updated_at
                                """,
                                (
                                    ikey,
                                    vrel.device_codename,
                                    vrel.model_code,
                                    vrel.region_code,
                                    vrel.canonical_version,
                                    vrel.package_type,
                                    orig_rel.conflict_signature,
                                    orig_rel.input_sha256,
                                    vrel.file_size_bytes,
                                    vrel.file_size_str,
                                    vrel.fast_cdn_url,
                                    vrel.md5_or_sha256,
                                    last_spooled,
                                    now_ts,
                                ),
                            )
                            existing_rows[ikey] = {
                                "identity_key": ikey,
                                "device_codename": vrel.device_codename,
                                "model_code": vrel.model_code,
                                "region_code": vrel.region_code,
                                "version_str": vrel.canonical_version,
                                "package_type": vrel.package_type,
                                "conflict_sig": orig_rel.conflict_signature,
                                "input_sha256": orig_rel.input_sha256,
                                "verify_state": "VERIFIED",
                                "file_size_bytes": vrel.file_size_bytes,
                                "file_size_str": vrel.file_size_str,
                                "verified_fast_url": vrel.fast_cdn_url,
                                "verified_checksum": vrel.md5_or_sha256,
                                "failure_count": 0,
                                "next_retry_at": 0,
                                "last_error": None,
                                "last_spooled_hash": last_spooled,
                                "updated_at": now_ts,
                            }
                            dev_verified_ready.append((vrel, last_spooled))
                        elif outcome.status == "DEAD_UPSTREAM":
                            probed_dead += 1
                            new_failures = prev_failures + 1
                            retry_at = now_ts + (
                                outcome.retry_after_seconds or DEAD_UPSTREAM_TTL_SECONDS
                            )
                            conn.execute(
                                """
                                INSERT INTO collector_releases (
                                    identity_key, device_codename, model_code, region_code, version_str,
                                    package_type, conflict_sig, input_sha256, verify_state,
                                    failure_count, next_retry_at, last_error, last_spooled_hash, updated_at
                                )
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'DEAD_UPSTREAM', ?, ?, ?, ?, ?)
                                ON CONFLICT(identity_key) DO UPDATE SET
                                    conflict_sig = excluded.conflict_sig,
                                    input_sha256 = excluded.input_sha256,
                                    verify_state = 'DEAD_UPSTREAM',
                                    failure_count = excluded.failure_count,
                                    next_retry_at = excluded.next_retry_at,
                                    last_error = excluded.last_error,
                                    updated_at = excluded.updated_at
                                """,
                                (
                                    ikey,
                                    orig_rel.device_codename,
                                    orig_rel.model_code,
                                    orig_rel.region_code,
                                    orig_rel.canonical_version,
                                    orig_rel.package_type,
                                    orig_rel.conflict_signature,
                                    orig_rel.input_sha256,
                                    new_failures,
                                    retry_at,
                                    outcome.reason,
                                    last_spooled,
                                    now_ts,
                                ),
                            )
                            existing_rows[ikey] = {
                                "identity_key": ikey,
                                "device_codename": orig_rel.device_codename,
                                "model_code": orig_rel.model_code,
                                "region_code": orig_rel.region_code,
                                "version_str": orig_rel.canonical_version,
                                "package_type": orig_rel.package_type,
                                "conflict_sig": orig_rel.conflict_signature,
                                "input_sha256": orig_rel.input_sha256,
                                "verify_state": "DEAD_UPSTREAM",
                                "file_size_bytes": None,
                                "file_size_str": None,
                                "verified_fast_url": None,
                                "verified_checksum": None,
                                "failure_count": new_failures,
                                "next_retry_at": retry_at,
                                "last_error": outcome.reason,
                                "last_spooled_hash": last_spooled,
                                "updated_at": now_ts,
                            }
                        else:
                            probed_deferred += 1
                            new_failures = prev_failures + 1
                            backoff = outcome.retry_after_seconds or compute_deferred_backoff(
                                prev_failures
                            )
                            retry_at = now_ts + backoff
                            conn.execute(
                                """
                                INSERT INTO collector_releases (
                                    identity_key, device_codename, model_code, region_code, version_str,
                                    package_type, conflict_sig, input_sha256, verify_state,
                                    failure_count, next_retry_at, last_error, last_spooled_hash, updated_at
                                )
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'DEFERRED_VERIFY', ?, ?, ?, ?, ?)
                                ON CONFLICT(identity_key) DO UPDATE SET
                                    conflict_sig = excluded.conflict_sig,
                                    input_sha256 = excluded.input_sha256,
                                    verify_state = 'DEFERRED_VERIFY',
                                    failure_count = excluded.failure_count,
                                    next_retry_at = excluded.next_retry_at,
                                    last_error = excluded.last_error,
                                    updated_at = excluded.updated_at
                                """,
                                (
                                    ikey,
                                    orig_rel.device_codename,
                                    orig_rel.model_code,
                                    orig_rel.region_code,
                                    orig_rel.canonical_version,
                                    orig_rel.package_type,
                                    orig_rel.conflict_signature,
                                    orig_rel.input_sha256,
                                    new_failures,
                                    retry_at,
                                    outcome.reason,
                                    last_spooled,
                                    now_ts,
                                ),
                            )
                            existing_rows[ikey] = {
                                "identity_key": ikey,
                                "device_codename": orig_rel.device_codename,
                                "model_code": orig_rel.model_code,
                                "region_code": orig_rel.region_code,
                                "version_str": orig_rel.canonical_version,
                                "package_type": orig_rel.package_type,
                                "conflict_sig": orig_rel.conflict_signature,
                                "input_sha256": orig_rel.input_sha256,
                                "verify_state": "DEFERRED_VERIFY",
                                "file_size_bytes": None,
                                "file_size_str": None,
                                "verified_fast_url": None,
                                "verified_checksum": None,
                                "failure_count": new_failures,
                                "next_retry_at": retry_at,
                                "last_error": outcome.reason,
                                "last_spooled_hash": last_spooled,
                                "updated_at": now_ts,
                            }
                    conn.commit()

                now_ts = clock()
                for vrel, last_spooled in dev_verified_ready:
                    if vrel.identity_key in collided_keys:
                        continue
                    spec = vrel.to_publish_spec(dev)
                    combined_hash = f"{spec.spec_sha256}:{spec.enrichment_sha256 or ''}"
                    if last_spooled == combined_hash:
                        unchanged_not_spooled += 1
                        continue
                    pending_outbox_specs.append(spec)
                    pending_spool_hashes.append((combined_hash, now_ts, spec.identity_key))

                if not device_truncated:
                    checked_ts = clock()
                    if not pending_outbox_specs:
                        conn.execute(
                            "UPDATE collector_devices SET last_checked_at = ? WHERE codename = ?",
                            (checked_ts, dev.codename),
                        )
                    else:
                        pending_checked_codenames.append((checked_ts, dev.codename))

                conn.commit()

                if len(pending_outbox_specs) >= outbox_batch_size:
                    _flush_outbox_buffer()

                if device_truncated:
                    break

            _flush_outbox_buffer()

            return {
                "vendor": vendor_key,
                "status": "ok",
                "devices_discovered": len(devices),
                "devices_checked": devices_checked,
                "device_fetch_errors": device_fetch_errors,
                "releases_seen": total_raw_releases,
                "unique_candidates": len(seen_candidates),
                "collisions_quarantined": collisions_quarantined,
                "probed_verified": probed_verified,
                "probed_dead": probed_dead,
                "probed_deferred": probed_deferred,
                "skipped_cached_probe": skipped_cached_probe,
                "skipped_backoff": skipped_backoff,
                "unchanged_not_spooled": unchanged_not_spooled,
                "spooled_specs": total_spooled_specs,
                "outbox_files_created": total_outbox_files_created,
            }
    finally:
        http_cache_conn.close()
        conn.close()


__all__ = ["COLLECTOR_SCHEMA_SQL", "run_collector"]
