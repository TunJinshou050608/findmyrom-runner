"""Explicit D1 Publisher for TG Custom Resources (v2 Section 3.6).

Completely abolishes full-cohort diff deletion (`public_token NOT IN keep`).
Only accepts explicit operations carrying `(resource_key, revision_id)`:
  - ADD: insert new resource into D1 custom_resources
  - UPDATE: update existing resource when revision_id & payload change
  - WITHDRAW: explicitly remove a specific resource_key at revision_id (respecting maintainer_opted_out)
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
import time
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from fmr_core.crypto import (
    build_custom_resource_payload,
    build_firmware_payload,
    verify_signature,
)
from .evidence_store import EvidenceStore
from .quality_audit import AUDIT_VERSION, publication_row, quality_errors, resource_token

VALID_OPS = ("ADD", "UPDATE", "WITHDRAW")


def compute_resource_key(device_codename: str, tg_post_url: str) -> str:
    return f"{device_codename.strip().lower()}|{tg_post_url.strip()}"


def enqueue_publish_operation(
    store: EvidenceStore,
    *,
    op_type: str,
    resource_key: str,
    revision_id: str,
    device_codename: str,
    tg_post_url: str,
    item: dict[str, Any] | None = None,
    withdraw_reason: str | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Enqueue an explicit ADD, UPDATE, or WITHDRAW operation with (resource_key, revision_id)."""
    op = op_type.strip().upper()
    if op not in VALID_OPS:
        raise ValueError(f"Invalid explicit publish op_type: {op_type!r}")
    if not resource_key or "|" not in resource_key:
        raise ValueError(f"Invalid resource_key: {resource_key!r}")
    if not revision_id or not str(revision_id).strip():
        raise ValueError("Explicit publish operation requires a non-empty revision_id")

    now_ts = time.time() if now is None else float(now)
    code = device_codename.strip().lower()
    url = tg_post_url.strip()
    pkg = (item or {}).get("package") or {"device_codename": code, "tg_post_url": url}
    token = resource_token(pkg)

    ledger_rows = store.query(
        "SELECT revision_id, payload_hash, status FROM tg_publish_ledger WHERE resource_key=?",
        (resource_key,),
    )
    if ledger_rows and ledger_rows[0]["status"] == "opted_out":
        return {"enqueued": False, "reason": "maintainer_opted_out", "resource_key": resource_key}

    if op in ("ADD", "UPDATE"):
        if not item or "package" not in item:
            raise ValueError(f"{op} operation requires item with package payload")
        payload_data = {
            "op_type": op,
            "resource_key": resource_key,
            "revision_id": revision_id,
            "package": item["package"],
            "evidence": item.get("evidence") or {},
        }
        p_hash = hashlib.sha256(
            json.dumps(item["package"], ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        if ledger_rows:
            current = ledger_rows[0]
            if current["status"] == "active" and (
                current["revision_id"] == revision_id or current["payload_hash"] == p_hash
            ):
                return {"enqueued": False, "reason": "already_active_same_payload", "resource_key": resource_key}
            op = "UPDATE"
        else:
            op = "ADD"
    else:
        payload_data = {
            "op_type": "WITHDRAW",
            "resource_key": resource_key,
            "revision_id": revision_id,
            "device_codename": code,
            "tg_post_url": url,
            "withdraw_reason": withdraw_reason or "explicit_withdrawal",
        }
        if ledger_rows and ledger_rows[0]["status"] == "withdrawn":
            return {"enqueued": False, "reason": "already_withdrawn", "resource_key": resource_key}

    store.execute(
        """
        INSERT INTO tg_publish_outbox (
            resource_key, revision_id, op_type, public_token,
            device_codename, payload_json, status, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
        ON CONFLICT(resource_key, revision_id, op_type) DO NOTHING
        """,
        (
            resource_key,
            revision_id,
            op,
            token,
            code,
            json.dumps(payload_data, ensure_ascii=False, sort_keys=True),
            now_ts,
        ),
    )
    return {
        "enqueued": True,
        "op_type": op,
        "resource_key": resource_key,
        "revision_id": revision_id,
        "public_token": token,
    }


def publish_explicit_outbox(
    store: EvidenceStore,
    db: Any,
    private_key_hex: str,
    *,
    max_operations: int = 100,
    run_id: str | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Consume pending explicit (resource_key, revision_id) operations from tg_publish_outbox into D1."""
    now_ts = time.time() if now is None else float(now)
    batch_run_id = str(run_id or int(now_ts * 1000))

    pending = store.query(
        """
        SELECT id, resource_key, revision_id, op_type, public_token, device_codename, payload_json
        FROM tg_publish_outbox
        WHERE status='pending'
        ORDER BY id ASC
        LIMIT ?
        """,
        (max_operations,),
    )
    if not pending:
        return {
            "status": "no_pending_operations",
            "audit_run_id": batch_run_id,
            "operations_processed": 0,
            "added": 0,
            "updated": 0,
            "withdrawn": 0,
            "skipped": 0,
        }

    key = private_key_hex.strip()
    if not re.fullmatch(r"[a-fA-F0-9]{64}", key):
        raise ValueError("Valid 64-hex Ed25519 signing key required")
    public_hex = (
        Ed25519PrivateKey.from_private_bytes(bytes.fromhex(key))
        .public_key()
        .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        .hex()
    )

    existing_fw = db.query(
        "SELECT public_token, fast_cdn_url, ed25519_signature FROM stock_firmwares ORDER BY id DESC LIMIT 10"
    )
    if existing_fw and not any(
        verify_signature(
            build_firmware_payload(r["public_token"], r["fast_cdn_url"]),
            r["ed25519_signature"],
            public_hex,
        )
        for r in existing_fw
    ):
        raise ValueError("Signing key does not match existing production firmware signatures")

    def select_in(table: str, field: str, values: list[str], projection: str = "*") -> list[dict[str, Any]]:
        dedup = list(dict.fromkeys(v for v in values if v))
        out_rows: list[dict[str, Any]] = []
        for offset in range(0, len(dedup), 80):
            chunk = dedup[offset : offset + 80]
            placeholders = ",".join("?" for _ in chunk)
            out_rows.extend(
                db.query(f"SELECT {projection} FROM {table} WHERE {field} IN ({placeholders})", chunk)
            )
        return out_rows

    known_devices = {
        r["codename"]
        for r in select_in(
            "devices",
            "codename",
            [row["device_codename"] for row in pending],
            "codename",
        )
    }
    existing_d1 = {
        r["public_token"]: r
        for r in select_in(
            "custom_resources",
            "public_token",
            [row["public_token"] for row in pending],
        )
    }

    columns = (
        "public_token,device_codename,zone_type,category_tag,project_name,is_official,"
        "android_version,build_version_tag,maintainer_name,maintainer_contact_tg,"
        "target_post_url,gapps_type,known_bugs_json,published_at,ed25519_signature,"
        "metadata_json,audit_batch_id"
    )
    updates = ",".join(f"{c}=excluded.{c}" for c in columns.split(",") if c != "public_token")
    upsert_sql = (
        f"INSERT INTO custom_resources({columns}) VALUES ({','.join('?' for _ in columns.split(','))}) "
        f"ON CONFLICT(public_token) DO UPDATE SET {updates} WHERE custom_resources.maintainer_opted_out=0"
    )

    added = updated = withdrawn = skipped = 0
    enabled_devices: set[str] = set()

    for row in pending:
        op_id = row["id"]
        op_type = row["op_type"]
        res_key = row["resource_key"]
        rev_id = row["revision_id"]
        token = row["public_token"]
        code = row["device_codename"]
        data = json.loads(row["payload_json"])
        d1_row = existing_d1.get(token)

        if d1_row and d1_row.get("maintainer_opted_out"):
            store.execute_batch(
                [
                    ("UPDATE tg_publish_outbox SET status='skipped_opted_out', committed_at=? WHERE id=?", (now_ts, op_id)),
                    (
                        """
                        INSERT INTO tg_publish_ledger (
                            resource_key, public_token, device_codename, target_post_url,
                            revision_id, payload_hash, status, updated_at
                        ) VALUES (?, ?, ?, ?, ?, '', 'opted_out', ?)
                        ON CONFLICT(resource_key) DO UPDATE SET status='opted_out', updated_at=excluded.updated_at
                        """,
                        (res_key, token, code, d1_row.get("target_post_url") or "", rev_id, now_ts),
                    ),
                ]
            )
            skipped += 1
            continue

        if op_type in ("ADD", "UPDATE"):
            pkg = data["package"]
            if code not in known_devices or quality_errors(pkg, True):
                store.execute(
                    "UPDATE tg_publish_outbox SET status='skipped_ineligible', committed_at=? WHERE id=?",
                    (now_ts, op_id),
                )
                skipped += 1
                continue

            values = publication_row(
                {"package": pkg, "evidence": data.get("evidence") or {}},
                key,
                batch_run_id,
            )
            if not verify_signature(
                build_custom_resource_payload(values[0], values[2], values[10]),
                values[14],
                public_hex,
            ):
                raise RuntimeError(f"Generated signature failed self-verification for {values[0]}")

            db.execute_batch([(upsert_sql, values)])
            enabled_devices.add(code)
            p_hash = hashlib.sha256(
                json.dumps(pkg, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest()
            if d1_row is None:
                added += 1
            else:
                updated += 1
            store.execute_batch(
                [
                    ("UPDATE tg_publish_outbox SET status='done', committed_at=? WHERE id=?", (now_ts, op_id)),
                    (
                        """
                        INSERT INTO tg_publish_ledger (
                            resource_key, public_token, device_codename, target_post_url,
                            revision_id, payload_hash, status, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?)
                        ON CONFLICT(resource_key) DO UPDATE SET
                            public_token=excluded.public_token,
                            revision_id=excluded.revision_id,
                            payload_hash=excluded.payload_hash,
                            status='active',
                            updated_at=excluded.updated_at
                        """,
                        (res_key, token, code, pkg["tg_post_url"], rev_id, p_hash, now_ts),
                    ),
                ]
            )
        elif op_type == "WITHDRAW":
            db.execute_batch(
                [
                    (
                        "DELETE FROM custom_resources WHERE public_token=? AND maintainer_opted_out=0",
                        (token,),
                    )
                ]
            )
            withdrawn += 1
            store.execute_batch(
                [
                    ("UPDATE tg_publish_outbox SET status='done', committed_at=? WHERE id=?", (now_ts, op_id)),
                    (
                        """
                        INSERT INTO tg_publish_ledger (
                            resource_key, public_token, device_codename, target_post_url,
                            revision_id, payload_hash, status, updated_at
                        ) VALUES (?, ?, ?, ?, ?, '', 'withdrawn', ?)
                        ON CONFLICT(resource_key) DO UPDATE SET
                            revision_id=excluded.revision_id,
                            status='withdrawn',
                            updated_at=excluded.updated_at
                        """,
                        (res_key, token, code, data.get("tg_post_url") or "", rev_id, now_ts),
                    ),
                ]
            )

    if enabled_devices:
        sorted_devs = sorted(enabled_devices)
        for offset in range(0, len(sorted_devs), 50):
            chunk = sorted_devs[offset : offset + 50]
            placeholders = ",".join("?" for _ in chunk)
            db.execute(
                f"UPDATE devices SET phase2_roms_enabled=1 WHERE phase2_roms_enabled!=1 AND codename IN ({placeholders})",
                chunk,
            )

    return {
        "status": "published",
        "audit_run_id": batch_run_id,
        "audit_version": AUDIT_VERSION,
        "operations_processed": len(pending),
        "added": added,
        "updated": updated,
        "withdrawn": withdrawn,
        "skipped": skipped,
        "enabled_devices": sorted(enabled_devices),
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
