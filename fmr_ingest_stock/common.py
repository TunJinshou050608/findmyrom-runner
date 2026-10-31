"""Bounded metadata loading, compatibility migration and source cursors."""

import hashlib
import os
from pathlib import Path
import re
import time

import httpx
import yaml

from fmr_core.crypto import sign_firmware, sign_payload
from .cdn_rewriter import rewrite_to_fast_cdn

MAX_SOURCE_BYTES = 52_428_800


def private_key():
    key = os.environ.get("SIGN_PRIVATE_KEY_HEX", "")
    if not re.fullmatch("[0-9a-f]{64}", key):
        raise ValueError("SIGN_PRIVATE_KEY_HEX must be 64 lowercase hex characters")
    sign_payload("key-validation", key)
    return key


def load_source(source):
    source = str(source)
    if source.lower().startswith(("http://", "https://")):
        rewrite_to_fast_cdn(
            "", source
        )  # Validate only; metadata hosts are not rewritten.
        for attempt in range(3):
            try:
                with httpx.stream(
                    "GET", source, timeout=30, follow_redirects=False
                ) as response:
                    response.raise_for_status()
                    data = bytearray()
                    for chunk in response.iter_bytes(chunk_size=64 * 1024):
                        data.extend(chunk)
                        if len(data) > MAX_SOURCE_BYTES:
                            raise ValueError("Metadata exceeds 50 MB")
                break
            except (httpx.TimeoutException, httpx.HTTPStatusError) as exc:
                if (
                    isinstance(exc, httpx.HTTPStatusError)
                    and exc.response.status_code < 500
                ) or attempt == 2:
                    raise
                time.sleep(0.25 * (attempt + 1))
    else:
        with Path(source).open("rb") as handle:
            data = handle.read(MAX_SOURCE_BYTES + 1)
        if len(data) > MAX_SOURCE_BYTES:
            raise ValueError("Metadata exceeds 50 MB")
    try:
        rows = yaml.safe_load(bytes(data))
    except (yaml.YAMLError, UnicodeError) as exc:
        raise ValueError("Invalid metadata YAML") from exc
    if not isinstance(rows, list):
        raise ValueError("Metadata root must be a list")
    return rows, hashlib.sha256(data).hexdigest()


def prepare_schema(db, key):
    columns = db.query("PRAGMA table_info(stock_firmwares)")
    if not columns:
        db.execute_script(
            Path(__file__).parents[1].joinpath("fmr_core/schema.sql").read_text()
        )
        return
    if "ed25519_signature" not in {c["name"] for c in columns}:
        db.execute(
            "ALTER TABLE stock_firmwares ADD COLUMN ed25519_signature TEXT NOT NULL DEFAULT ''"
        )
    # Also repairs a migration interrupted after adding the column.
    rows = db.query(
        "SELECT public_token, fast_cdn_url FROM stock_firmwares WHERE ed25519_signature = ''"
    )
    for offset in range(0, len(rows), 200):
        db.execute_batch(
            [
                (
                    "UPDATE stock_firmwares SET ed25519_signature=? WHERE public_token=?",
                    (
                        sign_firmware(r["public_token"], r["fast_cdn_url"], key),
                        r["public_token"],
                    ),
                )
                for r in rows[offset : offset + 200]
            ]
        )


def cursor_hash(db, kind, source):
    rows = db.query(
        "SELECT last_cursor_val FROM crawler_sources WHERE device_codename=? AND source_kind=? AND source_target=?",
        ("__global__", kind, str(source)),
    )
    return rows[0]["last_cursor_val"] if rows else None


def save_cursor(db, kind, source, digest):
    db.execute(
        """INSERT INTO crawler_sources
        (device_codename,source_kind,source_target,last_cursor_val,last_checked_at)
        VALUES ('__global__',?,?,?,?) ON CONFLICT(device_codename,source_kind,source_target)
        DO UPDATE SET last_cursor_val=excluded.last_cursor_val,last_checked_at=excluded.last_checked_at""",
        (kind, str(source), digest, int(time.time())),
    )


def text(value):
    return "" if value is None else str(value).strip()


def validate_batch_size(batch_size):
    if type(batch_size) is not int or not 50 <= batch_size <= 500:
        raise ValueError("batch_size must be between 50 and 500")
