"""Single-writer D1 Publisher with local SQLite Ledger and Hit-Quota-Stop-and-Checkpoint."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import datetime
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time
from typing import Any

from fmr_core.crypto import generate_public_token, sign_firmware
from fmr_core.d1_client import D1Client, D1QuotaExceededError
from fmr_core.extraction_policy import extraction_eligible
from fmr_core.publish_spec import PublishSpecV1
from fmr_ingest_stock.download_refresh import (
    PREFIX as REFRESH_PREFIX,
    fingerprint as refresh_fingerprint,
)

from .locks import LockBusyError, acquire_file_lock
from .outbox import DEFAULT_OUTBOX_BATCH_SIZE, OutboxSpool

LEDGER_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS ledger_entries (
    identity_key TEXT PRIMARY KEY,
    d1_id INTEGER,
    public_token TEXT NOT NULL,
    spec_sha256 TEXT,
    enrichment_sha256 TEXT,
    delivery_sha256 TEXT,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS publisher_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS quarantined_4tuple_collisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    identity_key TEXT NOT NULL,
    four_tuple_key TEXT NOT NULL,
    spec_json TEXT NOT NULL,
    detected_at INTEGER NOT NULL
);
"""

DEFAULT_BRAND_DISPLAY_NAMES: dict[str, str] = {
    "asus": "ASUS",
    "google": "Google",
    "infinix": "Infinix",
    "itel": "itel",
    "motorola": "Motorola",
    "nothing": "Nothing",
    "oneplus": "OnePlus",
    "oppo": "OPPO",
    "realme": "Realme",
    "rog": "ROG",
    "samsung": "Samsung",
    "sony": "Sony",
    "tecno": "TECNO",
    "vivo": "Vivo",
    "xiaomi": "Xiaomi",
}

BOOTSTRAP_PAGE_SIZE = 1000


@dataclass(frozen=True)
class _D1Capabilities:
    has_idx_fw_identity: bool
    has_firmware_metadata: bool
    has_firmware_delivery_specs: bool


def _default_utc_day() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")


class _UtcRolloverStop(Exception):
    """Internal control-flow signal when UTC day rolls over or approaches midnight."""

    def __init__(self, bootstrapped_rows: int = 0) -> None:
        super().__init__("UTC rollover brake triggered")
        self.bootstrapped_rows = bootstrapped_rows


def _should_stop_for_utc_rollover(
    day_clock: Callable[[], str],
    ts_clock: Callable[[], int],
    start_utc_day: str,
    stop_before_utc_midnight_seconds: int,
) -> bool:
    if day_clock() != start_utc_day:
        return True
    if stop_before_utc_midnight_seconds > 0:
        remaining_in_day = 86400 - (int(ts_clock()) % 86400)
        if remaining_in_day <= stop_before_utc_midnight_seconds:
            return True
    return False


_SONY_BUNDLE_PREFIX = "https://findmyrom.com/sony/bundles/"
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


def _compute_delivery_sha256(spec: PublishSpecV1) -> str | None:
    resolver_meta = spec.release.resolver_meta
    if resolver_meta is None:
        return None
    if isinstance(resolver_meta, Mapping):
        meta_dict = dict(resolver_meta)
        raw_source = (
            dict(meta_dict["source"])
            if "source" in meta_dict and isinstance(meta_dict["source"], Mapping)
            else meta_dict
        )
        if (
            spec.release.delivery_mode in ("resolver_fus", "resolver_oplus")
            or spec.release.fast_cdn_url.startswith(REFRESH_PREFIX)
        ):
            digest = refresh_fingerprint(raw_source)
            if (
                spec.release.fast_cdn_url.startswith(REFRESH_PREFIX)
                and spec.release.fast_cdn_url != REFRESH_PREFIX + digest
            ):
                raise ValueError(
                    "fast_cdn_url refresh hash does not match resolver_meta source fingerprint"
                )
            return digest
        if spec.release.fast_cdn_url.startswith(_SONY_BUNDLE_PREFIX):
            suffix = spec.release.fast_cdn_url[len(_SONY_BUNDLE_PREFIX) :]
            if _HEX64_RE.fullmatch(suffix):
                return suffix
    payload = {
        "delivery_mode": spec.release.delivery_mode,
        "resolver_meta": dict(resolver_meta) if isinstance(resolver_meta, Mapping) else resolver_meta,
    }
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _four_tuple_key(spec: PublishSpecV1) -> str:
    r = spec.release
    return json.dumps(
        [r.device_codename, r.region_code, r.version_str, r.package_type],
        separators=(",", ":"),
    )


def _open_ledger_db(ledger_path: Path) -> sqlite3.Connection:
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(ledger_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA busy_timeout = 5000;")
    conn.executescript(LEDGER_SCHEMA_SQL)
    cols = conn.execute("PRAGMA table_info(ledger_entries)").fetchall()
    if any(str(col["name"]) == "spec_sha256" and int(col["notnull"]) == 1 for col in cols):
        conn.executescript(
            """
            CREATE TABLE ledger_entries_v2 (
                identity_key TEXT PRIMARY KEY,
                d1_id INTEGER,
                public_token TEXT NOT NULL,
                spec_sha256 TEXT,
                enrichment_sha256 TEXT,
                delivery_sha256 TEXT,
                updated_at INTEGER NOT NULL
            );
            INSERT INTO ledger_entries_v2 (
                identity_key, d1_id, public_token, spec_sha256,
                enrichment_sha256, delivery_sha256, updated_at
            )
            SELECT
                identity_key, d1_id, public_token, spec_sha256,
                enrichment_sha256, delivery_sha256, updated_at
            FROM ledger_entries;
            DROP TABLE ledger_entries;
            ALTER TABLE ledger_entries_v2 RENAME TO ledger_entries;
            """
        )
    conn.commit()
    return conn


def _get_state(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM publisher_state WHERE key = ?", (key,)).fetchone()
    return str(row["value"]) if row is not None else default


def _set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO publisher_state (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value),
    )


def _delete_state(conn: sqlite3.Connection, key: str) -> None:
    conn.execute("DELETE FROM publisher_state WHERE key = ?", (key,))


def _detect_d1_capabilities(db: D1Client) -> _D1Capabilities:
    rows = db.query(
        """
        SELECT type, name FROM sqlite_master
        WHERE (type = 'index' AND name = 'idx_fw_identity')
           OR (type = 'table' AND name IN ('firmware_metadata', 'firmware_delivery_specs'))
        """
    )
    found = {(str(r["type"]), str(r["name"])) for r in rows}
    return _D1Capabilities(
        has_idx_fw_identity=("index", "idx_fw_identity") in found,
        has_firmware_metadata=("table", "firmware_metadata") in found,
        has_firmware_delivery_specs=("table", "firmware_delivery_specs") in found,
    )


def _bootstrap_ledger_from_d1(
    *,
    conn: sqlite3.Connection,
    db: D1Client,
    now_fn: Callable[[], int],
    bootstrap_page_size: int = BOOTSTRAP_PAGE_SIZE,
    stop_check: Callable[[], bool] | None = None,
) -> int:
    """Zero-write D1 pagination populating existing `public_token`s into `ledger_entries`."""
    last_id = int(_get_state(conn, "bootstrap_last_id", "0") or "0")
    bootstrapped_rows = 0

    while True:
        if stop_check is not None and stop_check():
            raise _UtcRolloverStop(bootstrapped_rows)
        rows = db.query(
            """
            SELECT
                id,
                public_token,
                device_codename,
                COALESCE(NULLIF(model_code, ''), device_codename) AS model_key,
                region_code,
                version_str,
                package_type,
                fast_cdn_url,
                md5_or_sha256,
                file_size_str
            FROM stock_firmwares
            WHERE id > ?
            ORDER BY id ASC
            LIMIT ?
            """,
            (last_id, bootstrap_page_size),
        )
        now_ts = now_fn()
        if rows:
            params: list[tuple[Any, ...]] = []
            for row in rows:
                identity_tuple = (
                    str(row["device_codename"]),
                    str(row["model_key"]),
                    str(row["region_code"]),
                    str(row["version_str"]),
                    str(row["package_type"]),
                )
                ikey = json.dumps(identity_tuple, separators=(",", ":"))
                params.append(
                    (
                        ikey,
                        int(row["id"]),
                        str(row["public_token"]),
                        "bootstrapped",
                        None,
                        None,
                        now_ts,
                    )
                )
                last_id = int(row["id"])

            conn.executemany(
                """
                INSERT INTO ledger_entries (
                    identity_key, d1_id, public_token, spec_sha256,
                    enrichment_sha256, delivery_sha256, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(identity_key) DO UPDATE SET
                    d1_id = excluded.d1_id,
                    public_token = excluded.public_token,
                    updated_at = excluded.updated_at
                """,
                params,
            )
            _set_state(conn, "bootstrap_last_id", str(last_id))
            conn.commit()
            bootstrapped_rows += len(rows)

        if len(rows) < bootstrap_page_size:
            _set_state(conn, "bootstrap_complete", "1")
            conn.commit()
            break

    return bootstrapped_rows


def _build_batch_statements_and_ledger_updates(
    *,
    specs: Sequence[PublishSpecV1],
    ledger_map: dict[str, dict[str, Any]],
    four_tuple_owners: dict[str, str],
    caps: _D1Capabilities,
    private_key_hex: str,
    now_ts: int,
    brand_names: Mapping[str, str],
    conn: sqlite3.Connection,
) -> tuple[list[tuple[str, Sequence[Any]]], list[tuple[Any, ...]], int]:
    """Construct atomic D1 batch SQL statements and matching ledger updates."""
    statements: list[tuple[str, Sequence[Any]]] = []
    ledger_updates: list[tuple[Any, ...]] = []
    quarantined_4tuple = 0

    seen_brands: set[str] = set()
    seen_devices: set[str] = set()

    publishable_specs: list[PublishSpecV1] = []
    for spec in specs:
        ikey = spec.identity_key
        if not caps.has_idx_fw_identity:
            ft_key = _four_tuple_key(spec)
            owner = four_tuple_owners.get(ft_key)
            if owner is not None and owner != ikey:
                conn.execute(
                    """
                    INSERT INTO quarantined_4tuple_collisions (
                        identity_key, four_tuple_key, spec_json, detected_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        ikey,
                        ft_key,
                        json.dumps(spec.to_dict(), sort_keys=True, separators=(",", ":")),
                        now_ts,
                    ),
                )
                quarantined_4tuple += 1
                continue
            four_tuple_owners[ft_key] = ikey
        publishable_specs.append(spec)

    if quarantined_4tuple:
        conn.commit()

    if not publishable_specs:
        return [], [], quarantined_4tuple

    delivery_hashes: dict[str, str | None] = {
        spec.identity_key: _compute_delivery_sha256(spec) for spec in publishable_specs
    }

    prejournal_rows: list[tuple[Any, ...]] = []
    for spec in publishable_specs:
        ikey = spec.identity_key
        if ikey not in ledger_map:
            pub_tok = generate_public_token("fw")
            prejournal_rows.append((ikey, None, pub_tok, None, None, None, now_ts))

    if prejournal_rows:
        conn.executemany(
            """
            INSERT OR IGNORE INTO ledger_entries (
                identity_key, d1_id, public_token, spec_sha256,
                enrichment_sha256, delivery_sha256, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            prejournal_rows,
        )
        conn.commit()
        for ikey, *_ in prejournal_rows:
            persisted = conn.execute(
                "SELECT * FROM ledger_entries WHERE identity_key = ?",
                (ikey,),
            ).fetchone()
            if persisted is not None:
                ledger_map[ikey] = dict(persisted)

    for spec in publishable_specs:
        brand_id = spec.device.brand_id
        if brand_id not in seen_brands:
            seen_brands.add(brand_id)
            brand_label = brand_names.get(brand_id) or DEFAULT_BRAND_DISPLAY_NAMES.get(
                brand_id, brand_id.capitalize()
            )
            statements.append(
                (
                    "INSERT OR IGNORE INTO brands (id, name, bl_unlock_status, sort_order) VALUES (?, ?, ?, ?)",
                    (brand_id, brand_label, "unlockable", 100),
                )
            )

    for spec in publishable_specs:
        dev = spec.device
        if dev.codename in seen_devices:
            continue
        seen_devices.add(dev.codename)
        statements.append(
            (
                """
                INSERT INTO devices (
                    codename, brand_id, marketing_name, model_numbers_json,
                    soc_name, boot_partition_type, has_vbmeta, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(codename) DO UPDATE SET
                    marketing_name = excluded.marketing_name,
                    model_numbers_json = excluded.model_numbers_json,
                    boot_partition_type = excluded.boot_partition_type,
                    updated_at = excluded.updated_at
                WHERE devices.marketing_name IS NOT excluded.marketing_name
                   OR devices.model_numbers_json IS NOT excluded.model_numbers_json
                   OR devices.boot_partition_type IS NOT excluded.boot_partition_type
                """,
                (
                    dev.codename,
                    dev.brand_id,
                    dev.marketing_name,
                    json.dumps(list(dev.model_numbers)),
                    dev.soc_name,
                    dev.boot_partition_type,
                    dev.has_vbmeta,
                    now_ts,
                ),
            )
        )

    conflict_clause = (
        "ON CONFLICT(device_codename, COALESCE(NULLIF(model_code, ''), device_codename), region_code, version_str, package_type)"
        if caps.has_idx_fw_identity
        else "ON CONFLICT(device_codename, region_code, version_str, package_type)"
    )

    fw_upsert_sql = f"""
        INSERT INTO stock_firmwares (
            public_token, device_codename, region_code, model_code, version_str,
            android_version, package_type, file_size_str, md5_or_sha256,
            original_official_url, fast_cdn_url, published_date,
            ed25519_signature, extract_status, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        {conflict_clause} DO UPDATE SET
            model_code = excluded.model_code,
            android_version = excluded.android_version,
            file_size_str = COALESCE(excluded.file_size_str, stock_firmwares.file_size_str),
            md5_or_sha256 = COALESCE(excluded.md5_or_sha256, stock_firmwares.md5_or_sha256),
            original_official_url = excluded.original_official_url,
            fast_cdn_url = excluded.fast_cdn_url,
            published_date = COALESCE(excluded.published_date, stock_firmwares.published_date),
            ed25519_signature = CASE
                WHEN stock_firmwares.fast_cdn_url IS NOT excluded.fast_cdn_url
                THEN excluded.ed25519_signature
                ELSE stock_firmwares.ed25519_signature
            END,
            extract_status = CASE
                WHEN stock_firmwares.fast_cdn_url IS NOT excluded.fast_cdn_url
                THEN excluded.extract_status
                ELSE stock_firmwares.extract_status
            END,
            extract_retries = CASE
                WHEN stock_firmwares.fast_cdn_url IS NOT excluded.fast_cdn_url
                THEN 0
                ELSE stock_firmwares.extract_retries
            END,
            extract_error = CASE
                WHEN stock_firmwares.fast_cdn_url IS NOT excluded.fast_cdn_url
                THEN NULL
                ELSE stock_firmwares.extract_error
            END,
            updated_at = excluded.updated_at
        WHERE stock_firmwares.model_code IS NOT excluded.model_code
           OR stock_firmwares.android_version IS NOT excluded.android_version
           OR stock_firmwares.file_size_str IS NOT COALESCE(excluded.file_size_str, stock_firmwares.file_size_str)
           OR stock_firmwares.md5_or_sha256 IS NOT COALESCE(excluded.md5_or_sha256, stock_firmwares.md5_or_sha256)
           OR stock_firmwares.original_official_url IS NOT excluded.original_official_url
           OR stock_firmwares.fast_cdn_url IS NOT excluded.fast_cdn_url
           OR stock_firmwares.published_date IS NOT COALESCE(excluded.published_date, stock_firmwares.published_date)
    """

    for spec in publishable_specs:
        ikey = spec.identity_key
        rel = spec.release
        dev = spec.device
        prior = ledger_map.get(ikey)

        public_token = str(prior["public_token"]) if prior is not None else generate_public_token("fw")
        d1_id = prior["d1_id"] if prior is not None else None

        signature = sign_firmware(
            public_token=public_token,
            fast_cdn_url=rel.fast_cdn_url,
            private_key_hex=private_key_hex,
        )
        ext_status = (
            "pending"
            if extraction_eligible(dev.brand_id, rel.package_type, rel.fast_cdn_url)
            else "skipped"
        )

        spec_hash = spec.spec_sha256
        enrich_hash = spec.enrichment_sha256
        delivery_hash = delivery_hashes[ikey]

        if prior is None or prior.get("spec_sha256") != spec_hash:
            statements.append(
                (
                    fw_upsert_sql,
                    (
                        public_token,
                        rel.device_codename,
                        rel.region_code,
                        rel.model_code,
                        rel.version_str,
                        rel.android_version,
                        rel.package_type,
                        rel.file_size_str,
                        rel.md5_or_sha256,
                        rel.original_official_url,
                        rel.fast_cdn_url,
                        rel.published_date,
                        signature,
                        ext_status,
                        now_ts,
                    ),
                )
            )

        effective_enrich_hash = prior.get("enrichment_sha256") if prior is not None else None
        if caps.has_firmware_metadata and enrich_hash is not None:
            if prior is None or prior.get("enrichment_sha256") != enrich_hash:
                meta_doc = spec.to_firmware_metadata_dict()
                assert meta_doc is not None
                meta_json = json.dumps(meta_doc, sort_keys=True, separators=(",", ":"))
                statements.append(
                    (
                        """
                        INSERT INTO firmware_metadata (
                            public_token, metadata_json, content_hash, updated_at
                        )
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(public_token) DO UPDATE SET
                            metadata_json = excluded.metadata_json,
                            content_hash = excluded.content_hash,
                            updated_at = excluded.updated_at
                        WHERE firmware_metadata.content_hash IS NOT excluded.content_hash
                        """,
                        (public_token, meta_json, enrich_hash, now_ts),
                    )
                )
            effective_enrich_hash = enrich_hash
        elif not caps.has_firmware_metadata:
            # Record enrichment_sha256 locally if table is absent so unchanged runs don't re-hit D1
            effective_enrich_hash = enrich_hash

        effective_delivery_hash = prior.get("delivery_sha256") if prior is not None else None
        if caps.has_firmware_delivery_specs and rel.resolver_meta is not None and delivery_hash is not None:
            if prior is None or prior.get("delivery_sha256") != delivery_hash:
                resolver_payload = (
                    dict(rel.resolver_meta)
                    if isinstance(rel.resolver_meta, Mapping)
                    else rel.resolver_meta
                )
                resolver_json = json.dumps(
                    resolver_payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
                )
                statements.append(
                    (
                        """
                        INSERT INTO firmware_delivery_specs (
                            public_token, delivery_mode, resolver_json, content_hash, updated_at
                        )
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(public_token) DO UPDATE SET
                            delivery_mode = excluded.delivery_mode,
                            resolver_json = excluded.resolver_json,
                            content_hash = excluded.content_hash,
                            updated_at = excluded.updated_at
                        WHERE firmware_delivery_specs.content_hash IS NOT excluded.content_hash
                        """,
                        (public_token, rel.delivery_mode, resolver_json, delivery_hash, now_ts),
                    )
                )
            effective_delivery_hash = delivery_hash
        elif not caps.has_firmware_delivery_specs:
            effective_delivery_hash = delivery_hash

        ledger_updates.append(
            (
                ikey,
                d1_id,
                public_token,
                spec_hash,
                effective_enrich_hash,
                effective_delivery_hash,
                now_ts,
            )
        )

    return statements, ledger_updates, quarantined_4tuple


def run_publisher(
    *,
    state_dir: str | Path,
    db: D1Client,
    private_key_hex: str,
    batch_size: int = DEFAULT_OUTBOX_BATCH_SIZE,
    bootstrap_page_size: int = BOOTSTRAP_PAGE_SIZE,
    utc_day_fn: Callable[[], str] | None = None,
    now_fn: Callable[[], int] | None = None,
    brand_names: Mapping[str, str] | None = None,
    stop_before_utc_midnight_seconds: int = 0,
) -> dict[str, Any]:
    """Drain `outbox/pending/*.jsonl` into D1 with Hit-Quota-Stop-and-Checkpoint."""
    if batch_size <= 0:
        raise ValueError(f"batch_size must be positive, got {batch_size}")
    if stop_before_utc_midnight_seconds < 0:
        raise ValueError(
            f"stop_before_utc_midnight_seconds must be non-negative, got {stop_before_utc_midnight_seconds}"
        )

    root = Path(state_dir)
    day_clock = utc_day_fn or _default_utc_day
    ts_clock = now_fn or (lambda: int(time.time()))
    lock_path = root / "locks" / "d1-publisher.lock"

    try:
        with acquire_file_lock(lock_path):
            return _execute_publisher_locked(
                root=root,
                db=db,
                private_key_hex=private_key_hex,
                batch_size=batch_size,
                bootstrap_page_size=bootstrap_page_size,
                day_clock=day_clock,
                ts_clock=ts_clock,
                brand_names=brand_names or DEFAULT_BRAND_DISPLAY_NAMES,
                stop_before_utc_midnight_seconds=stop_before_utc_midnight_seconds,
            )
    except LockBusyError:
        return {
            "status": "skipped_locked",
            "lock_path": str(lock_path),
            "published_specs": 0,
            "skipped_unchanged": 0,
        }


def _execute_publisher_locked(
    *,
    root: Path,
    db: D1Client,
    private_key_hex: str,
    batch_size: int,
    bootstrap_page_size: int,
    day_clock: Callable[[], str],
    ts_clock: Callable[[], int],
    brand_names: Mapping[str, str],
    stop_before_utc_midnight_seconds: int = 0,
) -> dict[str, Any]:
    ledger_path = root / "publisher_ledger.sqlite"
    conn = _open_ledger_db(ledger_path)
    outbox = OutboxSpool(root)

    start_utc_day = day_clock()
    current_utc_day = start_utc_day
    published_specs = 0
    skipped_unchanged = 0
    batches_committed = 0
    files_completed = 0
    bootstrapped_rows = 0
    quarantined_4tuple_total = 0

    try:
        # 1. Quota circuit breaker: 0 D1 calls if blocked for the current UTC day
        blocked_day = _get_state(conn, "blocked_until_utc_day")
        if blocked_day == current_utc_day:
            return {
                "status": "paused_d1_quota",
                "phase": "circuit_breaker",
                "utc_day": current_utc_day,
                "published_specs": 0,
                "skipped_unchanged": 0,
                "batches_committed": 0,
                "pending_files_remaining": len(outbox.list_pending()),
            }
        if blocked_day is not None:
            _delete_state(conn, "blocked_until_utc_day")
            conn.commit()

        # 2. Automatic zero-write D1 ledger bootstrap (resumable across quota days)
        if _get_state(conn, "bootstrap_complete") != "1":
            try:
                bootstrapped_rows = _bootstrap_ledger_from_d1(
                    conn=conn,
                    db=db,
                    now_fn=ts_clock,
                    bootstrap_page_size=bootstrap_page_size,
                    stop_check=lambda: _should_stop_for_utc_rollover(
                        day_clock,
                        ts_clock,
                        start_utc_day,
                        stop_before_utc_midnight_seconds,
                    ),
                )
            except _UtcRolloverStop as rstop:
                return {
                    "status": "paused_utc_rollover",
                    "phase": "bootstrap",
                    "utc_day": day_clock(),
                    "start_utc_day": start_utc_day,
                    "bootstrapped_rows": rstop.bootstrapped_rows,
                    "published_specs": 0,
                    "skipped_unchanged": 0,
                    "batches_committed": 0,
                    "pending_files_remaining": len(outbox.list_pending()),
                }
            except D1QuotaExceededError as qe:
                _set_state(conn, "blocked_until_utc_day", current_utc_day)
                conn.commit()
                return {
                    "status": "paused_d1_quota",
                    "phase": "bootstrap",
                    "utc_day": current_utc_day,
                    "bootstrapped_rows": bootstrapped_rows,
                    "published_specs": 0,
                    "skipped_unchanged": 0,
                    "batches_committed": 0,
                    "error": str(qe),
                    "pending_files_remaining": len(outbox.list_pending()),
                }

        pruned_done = outbox.prune_done(now_ts=float(ts_clock()))
        pending_files = outbox.list_pending()
        if not pending_files:
            return {
                "status": "ok",
                "utc_day": current_utc_day,
                "bootstrapped_rows": bootstrapped_rows,
                "published_specs": 0,
                "skipped_unchanged": 0,
                "batches_committed": 0,
                "files_completed": 0,
                "pruned_done_files": pruned_done,
                "pending_files_remaining": 0,
            }

        ledger_map: dict[str, dict[str, Any]] = {
            str(r["identity_key"]): dict(r)
            for r in conn.execute("SELECT * FROM ledger_entries").fetchall()
        }
        four_tuple_owners: dict[str, str] = {}
        for ikey in ledger_map:
            try:
                parts = json.loads(ikey)
                if isinstance(parts, list) and len(parts) == 5:
                    ft = json.dumps([parts[0], parts[2], parts[3], parts[4]], separators=(",", ":"))
                    four_tuple_owners.setdefault(ft, ikey)
            except Exception:
                continue

        caps: _D1Capabilities | None = None

        for pending_path in pending_files:
            file_specs = outbox.read_specs(pending_path)
            committed_in_file = 0

            try:
                for offset in range(0, len(file_specs), batch_size):
                    if _should_stop_for_utc_rollover(
                        day_clock,
                        ts_clock,
                        start_utc_day,
                        stop_before_utc_midnight_seconds,
                    ):
                        remaining_in_file = file_specs[committed_in_file:]
                        outbox.rewrite_uncommitted(pending_path, remaining_in_file)
                        return {
                            "status": "paused_utc_rollover",
                            "phase": "publish",
                            "utc_day": day_clock(),
                            "start_utc_day": start_utc_day,
                            "bootstrapped_rows": bootstrapped_rows,
                            "published_specs": published_specs,
                            "skipped_unchanged": skipped_unchanged,
                            "batches_committed": batches_committed,
                            "files_completed": files_completed,
                            "quarantined_4tuple_collisions": quarantined_4tuple_total,
                            "pending_files_remaining": len(outbox.list_pending()),
                        }

                    chunk = file_specs[offset : offset + batch_size]

                    # Deduplicate identical identity_keys within the same chunk (keep latest)
                    dedup_chunk_map: dict[str, PublishSpecV1] = {}
                    for spec in chunk:
                        dedup_chunk_map[spec.identity_key] = spec

                    dirty_specs: list[PublishSpecV1] = []
                    for spec in dedup_chunk_map.values():
                        prior = ledger_map.get(spec.identity_key)
                        delivery_hash = _compute_delivery_sha256(spec)
                        if (
                            prior is not None
                            and prior.get("spec_sha256") == spec.spec_sha256
                            and prior.get("enrichment_sha256") == spec.enrichment_sha256
                            and prior.get("delivery_sha256") == delivery_hash
                        ):
                            skipped_unchanged += 1
                        else:
                            dirty_specs.append(spec)

                    if dirty_specs:
                        if caps is None:
                            caps = _detect_d1_capabilities(db)

                        now_ts = ts_clock()
                        statements, ledger_updates, q4 = _build_batch_statements_and_ledger_updates(
                            specs=dirty_specs,
                            ledger_map=ledger_map,
                            four_tuple_owners=four_tuple_owners,
                            caps=caps,
                            private_key_hex=private_key_hex,
                            now_ts=now_ts,
                            brand_names=brand_names,
                            conn=conn,
                        )
                        quarantined_4tuple_total += q4

                        if statements:
                            db.execute_batch(statements)
                            batches_committed += 1

                        if ledger_updates:
                            conn.executemany(
                                """
                                INSERT INTO ledger_entries (
                                    identity_key, d1_id, public_token, spec_sha256,
                                    enrichment_sha256, delivery_sha256, updated_at
                                )
                                VALUES (?, ?, ?, ?, ?, ?, ?)
                                ON CONFLICT(identity_key) DO UPDATE SET
                                    d1_id = COALESCE(excluded.d1_id, ledger_entries.d1_id),
                                    public_token = excluded.public_token,
                                    spec_sha256 = excluded.spec_sha256,
                                    enrichment_sha256 = excluded.enrichment_sha256,
                                    delivery_sha256 = excluded.delivery_sha256,
                                    updated_at = excluded.updated_at
                                """,
                                ledger_updates,
                            )
                            conn.commit()
                            for (
                                ikey,
                                d1_id,
                                pub_tok,
                                s_hash,
                                e_hash,
                                d_hash,
                                upd_ts,
                            ) in ledger_updates:
                                ledger_map[ikey] = {
                                    "identity_key": ikey,
                                    "d1_id": d1_id,
                                    "public_token": pub_tok,
                                    "spec_sha256": s_hash,
                                    "enrichment_sha256": e_hash,
                                    "delivery_sha256": d_hash,
                                    "updated_at": upd_ts,
                                }
                            published_specs += len(ledger_updates)

                    committed_in_file += len(chunk)

                outbox.mark_done(pending_path)
                files_completed += 1

            except D1QuotaExceededError as qe:
                remaining_in_file = file_specs[committed_in_file:]
                outbox.rewrite_uncommitted(pending_path, remaining_in_file)
                _set_state(conn, "blocked_until_utc_day", current_utc_day)
                conn.commit()
                return {
                    "status": "paused_d1_quota",
                    "phase": "publish",
                    "utc_day": current_utc_day,
                    "bootstrapped_rows": bootstrapped_rows,
                    "published_specs": published_specs,
                    "skipped_unchanged": skipped_unchanged,
                    "batches_committed": batches_committed,
                    "files_completed": files_completed,
                    "quarantined_4tuple_collisions": quarantined_4tuple_total,
                    "error": str(qe),
                    "pending_files_remaining": len(outbox.list_pending()),
                }

        return {
            "status": "ok",
            "utc_day": current_utc_day,
            "bootstrapped_rows": bootstrapped_rows,
            "published_specs": published_specs,
            "skipped_unchanged": skipped_unchanged,
            "batches_committed": batches_committed,
            "files_completed": files_completed,
            "quarantined_4tuple_collisions": quarantined_4tuple_total,
            "pruned_done_files": pruned_done,
            "pending_files_remaining": len(outbox.list_pending()),
        }
    finally:
        conn.close()


def get_pipeline_status(
    *,
    state_dir: str | Path,
    vendors_yaml_path: str | Path | None = None,
) -> dict[str, Any]:
    """Inspect local collector SQLite states, Outbox queue depths, and Publisher Ledger state."""
    from fmr_ingest_stock.adapters import load_vendor_configs

    root = Path(state_dir)
    outbox = OutboxSpool(root)
    configs = load_vendor_configs(vendors_yaml_path)

    ledger_path = root / "publisher_ledger.sqlite"
    ledger_info: dict[str, Any] = {
        "exists": ledger_path.is_file(),
        "entries_count": 0,
        "bootstrap_complete": False,
        "blocked_until_utc_day": None,
    }
    if ledger_path.is_file():
        conn = _open_ledger_db(ledger_path)
        try:
            cnt = conn.execute("SELECT COUNT(*) AS c FROM ledger_entries").fetchone()
            ledger_info["entries_count"] = int(cnt["c"]) if cnt else 0
            ledger_info["bootstrap_complete"] = _get_state(conn, "bootstrap_complete") == "1"
            ledger_info["blocked_until_utc_day"] = _get_state(conn, "blocked_until_utc_day")
        finally:
            conn.close()

    vendors_summary: dict[str, Any] = {}
    for vid, cfg in sorted(configs.items()):
        vdb = root / "collectors" / f"{vid}.sqlite"
        entry: dict[str, Any] = {
            "enabled": cfg.enabled,
            "status": cfg.status,
            "bucket": cfg.bucket,
            "collector_db_exists": vdb.is_file(),
        }
        if vdb.is_file():
            vconn = sqlite3.connect(str(vdb))
            vconn.row_factory = sqlite3.Row
            try:
                dev_cnt = vconn.execute("SELECT COUNT(*) AS c FROM collector_devices").fetchone()
                rel_cnt = vconn.execute("SELECT COUNT(*) AS c FROM collector_releases").fetchone()
                col_cnt = vconn.execute("SELECT COUNT(*) AS c FROM quarantined_collisions").fetchone()
                entry["devices"] = int(dev_cnt["c"]) if dev_cnt else 0
                entry["releases"] = int(rel_cnt["c"]) if rel_cnt else 0
                entry["quarantined_collisions"] = int(col_cnt["c"]) if col_cnt else 0
            except sqlite3.OperationalError:
                pass
            finally:
                vconn.close()
        vendors_summary[vid] = entry

    return {
        "state_dir": str(root),
        "outbox": {
            "pending_files": len(outbox.list_pending()),
            "done_files": len(outbox.list_done()),
        },
        "publisher_ledger": ledger_info,
        "vendors": vendors_summary,
    }


__all__ = [
    "BOOTSTRAP_PAGE_SIZE",
    "DEFAULT_BRAND_DISPLAY_NAMES",
    "LEDGER_SCHEMA_SQL",
    "get_pipeline_status",
    "run_publisher",
]
