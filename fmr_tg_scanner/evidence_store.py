"""103 SQLite v2 Evidence Store, Exact 3-Rule Deduplication, 5% Blindspot Sampling,
Same-Transaction Incremental Queue, and Smooth 67k Backlog Migration (v2 Sections 3.3 & 3.6).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import shutil
import sqlite3
import time
from typing import Any
from urllib.parse import urlsplit
import zlib

from .html_parser import ChannelUnavailableError, parse_tg_channel_page
from .storage import SQLiteDB

EVIDENCE_SCHEMA_VERSION = "tg-evidence-v2.0"
BLINDSPOT_SAMPLE_MODULO = 20  # 1/20 == 5.0% deterministic blindspot sample

RELEASE_SIGNAL_RE = re.compile(
    r"(?i)\b(?:rom|kernel|recovery|twrp|orangefox|ofrp|pbrp|shrp|boot|init_boot|vendor_boot|"
    r"firmware|fw|build|release|updated?|changelog|download|flash|gapps|vanilla|unofficial|"
    r"official|lineage|crdroid|evolution|pixelexperience|pixelos|derpfest|cherish|hyperos|"
    r"miui|oxygenos|coloros|oneui|aosp|gsi|port)\b|发布|更新|刷机|内核|底包|移植",
)

V2_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tg_raw_messages (
    post_key TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL,
    channel_username TEXT NOT NULL,
    msg_id INTEGER NOT NULL,
    tg_post_url TEXT NOT NULL,
    published_timestamp INTEGER,
    first_observed_at REAL NOT NULL,
    last_observed_at REAL NOT NULL,
    raw_text TEXT NOT NULL,
    attachment_names_json TEXT NOT NULL,
    link_items_json TEXT NOT NULL,
    reply_context_json TEXT NOT NULL,
    forward_context_json TEXT NOT NULL,
    media_group_context_json TEXT NOT NULL,
    sender_context_json TEXT NOT NULL,
    content_revision_hash TEXT NOT NULL,
    shared_body_hash TEXT NOT NULL,
    content_revision_no INTEGER NOT NULL DEFAULT 1,
    process_status TEXT NOT NULL,
    process_version TEXT NOT NULL,
    process_reason TEXT NOT NULL,
    is_blindspot_sample INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tg_raw_shared_body ON tg_raw_messages(shared_body_hash);
CREATE INDEX IF NOT EXISTS idx_tg_raw_status ON tg_raw_messages(process_status, last_observed_at);

CREATE TABLE IF NOT EXISTS tg_cleaning_queue (
    post_key TEXT PRIMARY KEY,
    content_revision_hash TEXT NOT NULL,
    shared_body_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_retry_at REAL NOT NULL DEFAULT 0,
    last_error TEXT,
    enqueued_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tg_cleaning_due ON tg_cleaning_queue(status, next_retry_at, enqueued_at);

CREATE TABLE IF NOT EXISTS tg_extraction_ledger (
    extraction_cache_key TEXT PRIMARY KEY,
    shared_body_hash TEXT NOT NULL,
    model_name TEXT NOT NULL,
    spec_version TEXT NOT NULL,
    raw_model_json TEXT NOT NULL,
    normalized_json TEXT NOT NULL,
    published_json TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tg_extraction_body ON tg_extraction_ledger(shared_body_hash, spec_version);

CREATE TABLE IF NOT EXISTS tg_pending_evidence_queue (
    post_key TEXT PRIMARY KEY,
    device_codename TEXT,
    project_name TEXT,
    missing_fields_json TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS tg_publish_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    resource_key TEXT NOT NULL,
    revision_id TEXT NOT NULL,
    op_type TEXT NOT NULL,
    public_token TEXT NOT NULL,
    device_codename TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at REAL NOT NULL,
    committed_at REAL,
    UNIQUE(resource_key, revision_id, op_type)
);
CREATE INDEX IF NOT EXISTS idx_tg_outbox_pending ON tg_publish_outbox(status, id);

CREATE TABLE IF NOT EXISTS tg_publish_ledger (
    resource_key TEXT PRIMARY KEY,
    public_token TEXT NOT NULL,
    device_codename TEXT NOT NULL,
    target_post_url TEXT NOT NULL,
    revision_id TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    updated_at REAL NOT NULL
);
"""


def _norm_whitespace(text: str) -> str:
    return " ".join((text or "").split())


def _canonical_attachments(message: dict[str, Any]) -> list[str]:
    names = list(message.get("attachment_names") or [])
    single = message.get("file_attachment_name")
    if single and single not in names:
        names.append(single)
    return [str(n).strip() for n in names if str(n).strip()]


def _canonical_link_items(message: dict[str, Any]) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for entry in message.get("link_items") or []:
        if isinstance(entry, dict) and entry.get("url"):
            url = str(entry["url"]).strip()
            label = str(entry.get("label") or "").strip()
            if (url, label) not in seen:
                seen.add((url, label))
                items.append({"url": url, "label": label})
    for url in message.get("links") or []:
        url_str = str(url).strip()
        if url_str and not any(i["url"] == url_str for i in items):
            items.append({"url": url_str, "label": ""})
    return items


def compute_content_revision_hash(message: dict[str, Any]) -> str:
    """Exact post revision hash (Rules 1 & 3).

    Any edit to text, version numbers, device names, attachment names, actual link URLs,
    or reply target produces a new content_revision_hash.
    """
    text = (message.get("text") or "").strip()
    attachments = _canonical_attachments(message)
    links = [item["url"] for item in _canonical_link_items(message)]
    reply_target = message.get("reply_to_post_url") or message.get("reply_to_msg_id")
    payload = [text, attachments, links, reply_target]
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def compute_shared_body_hash(message: dict[str, Any]) -> str:
    """Cross-channel forward content hash (Rule 2).

    Identical message body, attachment names, and non-Telegram external download links
    produce the same shared_body_hash so semantic extraction can be reused while
    keeping each channel's post URL, forwarder, and author provenance distinct.
    """
    norm_text = _norm_whitespace(message.get("text") or "")
    attachments = _canonical_attachments(message)
    external_links = sorted(
        {
            item["url"]
            for item in _canonical_link_items(message)
            if not re.match(r"(?i)^https?://(?:t\.me|telegram\.me)/", item["url"])
        }
    )
    payload = [norm_text, attachments, external_links]
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def is_blindspot_sample(post_key: str, modulo: int = BLINDSPOT_SAMPLE_MODULO) -> bool:
    """Deterministic ~5% sampling of basic-rule-rejected posts for blindspot monitoring."""
    digest = hashlib.sha256(post_key.encode("utf-8")).digest()
    return (int.from_bytes(digest[:4], "big") % max(1, modulo)) == 0


def evaluate_basic_hard_rules(message: dict[str, Any]) -> tuple[bool, str]:
    """Basic hard-rule pre-filter before the local ONNX 3-class classifier.

    Returns (passed, reason). Posts with attachments, external links, or reply context
    with release keywords pass through even when short.
    """
    text = (message.get("text") or "").strip()
    attachments = _canonical_attachments(message)
    link_items = _canonical_link_items(message)
    external_links = [
        item["url"]
        for item in link_items
        if not re.match(r"(?i)^https?://(?:t\.me|telegram\.me)/", item["url"])
    ]
    reply_ctx = message.get("reply_to_msg_id") or message.get("reply_to_post_url")

    if not text and not attachments and not external_links:
        return False, "empty_post_without_media_or_links"

    if attachments or external_links:
        return True, "has_attachment_or_external_link"

    if reply_ctx and len(text) >= 4:
        return True, "has_reply_context"

    if len(text) < 15 and not RELEASE_SIGNAL_RE.search(text):
        return False, "too_short_without_release_signal"

    return True, "passed_hard_rules"


class EvidenceStore(SQLiteDB):
    """v2 SQLite Evidence Ledger and Same-Transaction Incremental Queue."""

    def __init__(self, path: str | Path) -> None:
        super().__init__(path)
        self.execute_script(V2_SCHEMA_SQL)

    def ingest_message(
        self,
        message: dict[str, Any],
        *,
        now: float | None = None,
        enqueue_for_cleaning: bool = True,
        initial_status: str | None = None,
        process_version: str = EVIDENCE_SCHEMA_VERSION,
    ) -> dict[str, Any]:
        """Atomically persist a raw TG message and enqueue it into tg_cleaning_queue in one transaction.

        Enforces the 3 Deduplication Rules:
        - Rule 1: Same post & same content_revision_hash -> 0 new model calls, 0 new queue writes.
        - Rule 2: Different channel forwarding identical body -> shared_body_hash recorded for semantic reuse,
                  while post_key, tg_post_url, forward_context_json, and sender_context_json remain distinct.
        - Rule 3: Changed version, device, or link -> content_revision_hash changes -> increments revision_no
                  and re-enqueues in the same transaction.
        """
        now_ts = time.time() if now is None else float(now)
        channel = str(message["channel_username"]).strip().lower().lstrip("@")
        msg_id = int(message["msg_id"])
        post_key = f"{channel}|{msg_id}"
        post_url = message.get("tg_post_url") or f"https://t.me/{channel}/{msg_id}"
        channel_id = str(message.get("channel_id") or f"@{channel}")

        rev_hash = compute_content_revision_hash(message)
        body_hash = compute_shared_body_hash(message)

        existing = self.query(
            "SELECT content_revision_hash, content_revision_no, process_status, first_observed_at "
            "FROM tg_raw_messages WHERE post_key=?",
            (post_key,),
        )
        if existing and existing[0]["content_revision_hash"] == rev_hash:
            return {
                "post_key": post_key,
                "action": "unchanged_revision_reused",
                "content_revision_hash": rev_hash,
                "shared_body_hash": body_hash,
                "enqueued": False,
                "process_status": existing[0]["process_status"],
            }

        passed_hard, hard_reason = evaluate_basic_hard_rules(message)
        blindspot = 0
        if initial_status is not None:
            status = initial_status
            reason = "initial_migration"
            should_enqueue = False
        elif passed_hard:
            status = "pending"
            reason = hard_reason
            should_enqueue = bool(enqueue_for_cleaning)
        else:
            if is_blindspot_sample(post_key):
                blindspot = 1
                status = "blindspot_retained"
                reason = f"blindspot_5pct:{hard_reason}"
                should_enqueue = False
            else:
                status = "hard_filtered"
                reason = hard_reason
                should_enqueue = False

        rev_no = (int(existing[0]["content_revision_no"]) + 1) if existing else 1
        first_obs = float(existing[0]["first_observed_at"]) if existing else now_ts

        attachments_json = json.dumps(_canonical_attachments(message), ensure_ascii=False)
        links_json = json.dumps(_canonical_link_items(message), ensure_ascii=False)
        reply_json = json.dumps(
            {
                "reply_to_post_url": message.get("reply_to_post_url"),
                "reply_to_msg_id": message.get("reply_to_msg_id"),
                "reply_author": message.get("reply_author"),
                "reply_text": message.get("reply_text"),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        forward_json = json.dumps(
            {
                "has_forward_header": bool(message.get("has_forward_header")),
                "forwarded_from_url": message.get("forwarded_from_url"),
                "forwarded_from_name": message.get("forwarded_from_name"),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        group_json = json.dumps(
            {
                "media_group_id": message.get("media_group_id"),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        sender_json = json.dumps(
            {
                "sender_author_signature": message.get("sender_author_signature"),
                "sender_username": message.get("sender_username"),
                "sender_id": message.get("sender_id"),
            },
            ensure_ascii=False,
            sort_keys=True,
        )

        statements: list[tuple[str, tuple[Any, ...]]] = [
            (
                """
                INSERT INTO tg_raw_messages (
                    post_key, channel_id, channel_username, msg_id, tg_post_url,
                    published_timestamp, first_observed_at, last_observed_at,
                    raw_text, attachment_names_json, link_items_json,
                    reply_context_json, forward_context_json, media_group_context_json,
                    sender_context_json, content_revision_hash, shared_body_hash,
                    content_revision_no, process_status, process_version,
                    process_reason, is_blindspot_sample
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(post_key) DO UPDATE SET
                    channel_id=excluded.channel_id,
                    channel_username=excluded.channel_username,
                    last_observed_at=excluded.last_observed_at,
                    raw_text=excluded.raw_text,
                    attachment_names_json=excluded.attachment_names_json,
                    link_items_json=excluded.link_items_json,
                    reply_context_json=excluded.reply_context_json,
                    forward_context_json=excluded.forward_context_json,
                    media_group_context_json=excluded.media_group_context_json,
                    sender_context_json=excluded.sender_context_json,
                    content_revision_hash=excluded.content_revision_hash,
                    shared_body_hash=excluded.shared_body_hash,
                    content_revision_no=excluded.content_revision_no,
                    process_status=excluded.process_status,
                    process_version=excluded.process_version,
                    process_reason=excluded.process_reason,
                    is_blindspot_sample=excluded.is_blindspot_sample
                """,
                (
                    post_key,
                    channel_id,
                    channel,
                    msg_id,
                    post_url,
                    message.get("published_timestamp"),
                    first_obs,
                    now_ts,
                    message.get("text") or "",
                    attachments_json,
                    links_json,
                    reply_json,
                    forward_json,
                    group_json,
                    sender_json,
                    rev_hash,
                    body_hash,
                    rev_no,
                    status,
                    process_version,
                    reason,
                    blindspot,
                ),
            )
        ]

        if should_enqueue:
            statements.append(
                (
                    """
                    INSERT INTO tg_cleaning_queue (
                        post_key, content_revision_hash, shared_body_hash,
                        status, attempts, next_retry_at, last_error, enqueued_at, updated_at
                    ) VALUES (?, ?, ?, 'pending', 0, 0, NULL, ?, ?)
                    ON CONFLICT(post_key) DO UPDATE SET
                        content_revision_hash=excluded.content_revision_hash,
                        shared_body_hash=excluded.shared_body_hash,
                        status='pending',
                        attempts=0,
                        next_retry_at=0,
                        last_error=NULL,
                        updated_at=excluded.updated_at
                    """,
                    (post_key, rev_hash, body_hash, now_ts, now_ts),
                )
            )

        self.execute_batch(statements)
        return {
            "post_key": post_key,
            "action": "updated_revision" if existing else "inserted",
            "content_revision_hash": rev_hash,
            "shared_body_hash": body_hash,
            "content_revision_no": rev_no,
            "enqueued": should_enqueue,
            "process_status": status,
            "is_blindspot_sample": bool(blindspot),
        }

    def fetch_due_cleaning_batch(
        self,
        *,
        limit: int = 50,
        now: float | None = None,
    ) -> list[dict[str, Any]]:
        now_ts = time.time() if now is None else float(now)
        return self.query(
            """
            SELECT q.post_key, q.content_revision_hash, q.shared_body_hash,
                   q.status, q.attempts, m.channel_id, m.channel_username,
                   m.msg_id, m.tg_post_url, m.published_timestamp, m.raw_text,
                   m.attachment_names_json, m.link_items_json, m.reply_context_json,
                   m.forward_context_json, m.media_group_context_json, m.sender_context_json,
                   m.content_revision_no
            FROM tg_cleaning_queue q
            JOIN tg_raw_messages m ON m.post_key = q.post_key
            WHERE q.status IN ('pending', 'retry_wait') AND q.next_retry_at <= ?
            ORDER BY q.enqueued_at ASC
            LIMIT ?
            """,
            (now_ts, limit),
        )

    def mark_cleaning_retry(
        self,
        post_key: str,
        error: str,
        *,
        cooldown_seconds: int = 900,
        now: float | None = None,
    ) -> None:
        """Record transient API failure (timeout, 429, 401/403, 5xx) without marking irrelevant or withdrawing."""
        now_ts = time.time() if now is None else float(now)
        self.execute_batch(
            [
                (
                    """
                    UPDATE tg_cleaning_queue
                    SET status='retry_wait',
                        attempts=attempts+1,
                        next_retry_at=?,
                        last_error=?,
                        updated_at=?
                    WHERE post_key=?
                    """,
                    (now_ts + cooldown_seconds, error[:500], now_ts, post_key),
                ),
                (
                    """
                    UPDATE tg_raw_messages
                    SET process_status='retry_wait',
                        process_reason=?
                    WHERE post_key=?
                    """,
                    (f"retry_wait:{error[:200]}", post_key),
                ),
            ]
        )

    def mark_cleaning_result(
        self,
        post_key: str,
        *,
        process_status: str,
        process_reason: str,
        process_version: str = EVIDENCE_SCHEMA_VERSION,
        queue_status: str = "done",
        is_blindspot: bool | None = None,
        now: float | None = None,
    ) -> None:
        """Atomically record a non-retry v2 cleaning decision on raw message and queue."""
        now_ts = time.time() if now is None else float(now)
        rows = self.query(
            "SELECT content_revision_hash, shared_body_hash FROM tg_raw_messages WHERE post_key=?",
            (post_key,),
        )
        if is_blindspot is None:
            raw_sql = """
                UPDATE tg_raw_messages
                SET process_status=?,
                    process_version=?,
                    process_reason=?
                WHERE post_key=?
            """
            raw_params: tuple[Any, ...] = (process_status, process_version, process_reason[:300], post_key)
        else:
            raw_sql = """
                UPDATE tg_raw_messages
                SET process_status=?,
                    process_version=?,
                    process_reason=?,
                    is_blindspot_sample=?
                WHERE post_key=?
            """
            raw_params = (
                process_status,
                process_version,
                process_reason[:300],
                1 if is_blindspot else 0,
                post_key,
            )
        statements: list[tuple[str, tuple[Any, ...]]] = [(raw_sql, raw_params)]
        if rows:
            statements.append(
                (
                    """
                    INSERT INTO tg_cleaning_queue (
                        post_key, content_revision_hash, shared_body_hash,
                        status, attempts, next_retry_at, last_error, enqueued_at, updated_at
                    ) VALUES (?, ?, ?, ?, 0, 0, NULL, ?, ?)
                    ON CONFLICT(post_key) DO UPDATE SET
                        status=excluded.status,
                        next_retry_at=0,
                        last_error=NULL,
                        updated_at=excluded.updated_at
                    """,
                    (
                        post_key,
                        rows[0]["content_revision_hash"],
                        rows[0]["shared_body_hash"],
                        queue_status,
                        now_ts,
                        now_ts,
                    ),
                )
            )
        self.execute_batch(statements)

    def fetch_backlog_batch(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """Fetch historical uncleaned_backlog posts newest-first without flooding real-time queue."""
        if limit <= 0:
            return []
        return self.query(
            """
            SELECT m.post_key, m.content_revision_hash, m.shared_body_hash,
                   'uncleaned_backlog' AS status, 0 AS attempts,
                   m.channel_id, m.channel_username, m.msg_id, m.tg_post_url,
                   m.published_timestamp, m.raw_text, m.attachment_names_json,
                   m.link_items_json, m.reply_context_json, m.forward_context_json,
                   m.media_group_context_json, m.sender_context_json,
                   m.content_revision_no
            FROM tg_raw_messages m
            WHERE m.process_status='uncleaned_backlog'
              AND (m.raw_text != '' OR m.attachment_names_json != '[]' OR m.link_items_json != '[]')
            ORDER BY m.last_observed_at DESC
            LIMIT ?
            """,
            (limit,),
        )

    def upsert_pending_evidence(
        self,
        post_key: str,
        *,
        device_codename: str | None,
        project_name: str | None,
        missing_fields: list[str],
        now: float | None = None,
    ) -> None:
        now_ts = time.time() if now is None else float(now)
        self.execute(
            """
            INSERT INTO tg_pending_evidence_queue (
                post_key, device_codename, project_name, missing_fields_json, created_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(post_key) DO UPDATE SET
                device_codename=excluded.device_codename,
                project_name=excluded.project_name,
                missing_fields_json=excluded.missing_fields_json,
                created_at=excluded.created_at
            """,
            (
                post_key,
                device_codename,
                project_name,
                json.dumps(sorted(set(missing_fields)), ensure_ascii=False),
                now_ts,
            ),
        )

    def clear_pending_evidence(self, post_key: str) -> None:
        self.execute("DELETE FROM tg_pending_evidence_queue WHERE post_key=?", (post_key,))

    def get_extraction_ledger(self, extraction_cache_key: str) -> dict[str, Any] | None:
        rows = self.query(
            """
            SELECT extraction_cache_key, shared_body_hash, model_name, spec_version,
                   raw_model_json, normalized_json, published_json, updated_at
            FROM tg_extraction_ledger
            WHERE extraction_cache_key=?
            """,
            (extraction_cache_key,),
        )
        if not rows:
            return None
        row = dict(rows[0])
        for col in ("raw_model_json", "normalized_json", "published_json"):
            try:
                row[col.removesuffix("_json")] = json.loads(row[col])
            except ValueError:
                row[col.removesuffix("_json")] = None
        return row

    def put_extraction_ledger(
        self,
        *,
        extraction_cache_key: str,
        shared_body_hash: str,
        model_name: str,
        spec_version: str,
        raw_model: dict[str, Any],
        normalized: dict[str, Any],
        published: dict[str, Any] | list[Any],
        now: float | None = None,
    ) -> None:
        now_ts = time.time() if now is None else float(now)
        self.execute(
            """
            INSERT INTO tg_extraction_ledger (
                extraction_cache_key, shared_body_hash, model_name, spec_version,
                raw_model_json, normalized_json, published_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(extraction_cache_key) DO UPDATE SET
                shared_body_hash=excluded.shared_body_hash,
                model_name=excluded.model_name,
                spec_version=excluded.spec_version,
                raw_model_json=excluded.raw_model_json,
                normalized_json=excluded.normalized_json,
                published_json=excluded.published_json,
                updated_at=excluded.updated_at
            """,
            (
                extraction_cache_key,
                shared_body_hash,
                model_name,
                spec_version,
                json.dumps(raw_model, ensure_ascii=False, sort_keys=True),
                json.dumps(normalized, ensure_ascii=False, sort_keys=True),
                json.dumps(published, ensure_ascii=False, sort_keys=True),
                now_ts,
            ),
        )

    def load_channel_profiles(self) -> dict[str, str]:
        """Load synced tg_search_meta channel profiles (`profile:<channel>`) if table exists."""
        has_meta = self.query(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tg_search_meta'"
        )
        if not has_meta:
            return {}
        profiles: dict[str, str] = {}
        for row in self.query("SELECT key, value FROM tg_search_meta WHERE key LIKE 'profile:%'"):
            channel = str(row["key"])[len("profile:") :].strip().lower().lstrip("@")
            if not channel:
                continue
            raw_val = row["value"]
            try:
                parsed = json.loads(raw_val) if isinstance(raw_val, str) else raw_val
            except ValueError:
                parsed = raw_val
            if isinstance(parsed, str) and parsed.strip():
                profiles[channel] = parsed
        return profiles

    def sync_raw_and_queue_from_snapshot(self, snap_db_path: str | Path) -> dict[str, int]:
        """Safely merge tg_raw_messages & pending tg_cleaning_queue rows from a backup snapshot.

        Never overwrites an already-processed clean status when content_revision_hash matches.
        """
        return sync_raw_and_queue_from_snapshot(self, snap_db_path)


def sync_raw_and_queue_from_snapshot(store: SQLiteDB, snap_db_path: str | Path) -> dict[str, int]:
    """Merge tg_raw_messages, pending tg_cleaning_queue, and tg_search_meta profiles from a backup snapshot."""
    stats = {"raw_synced": 0, "queue_synced": 0, "profiles_synced": 0}
    with sqlite3.connect(f"file:{Path(snap_db_path).resolve()}?mode=ro", uri=True, timeout=15.0) as db:
        db.row_factory = sqlite3.Row
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "tg_search_meta" in tables:
            meta_rows = db.execute(
                "SELECT key, value FROM tg_search_meta WHERE key LIKE 'profile:%' OR key='device_aliases_v3'"
            ).fetchall()
            if meta_rows:
                store.execute(
                    "CREATE TABLE IF NOT EXISTS tg_search_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
                )
                store.execute_batch(
                    [
                        (
                            "INSERT INTO tg_search_meta(key, value) VALUES (?, ?) "
                            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            (row["key"], row["value"]),
                        )
                        for row in meta_rows
                    ]
                )
                stats["profiles_synced"] = len(meta_rows)
        if "tg_raw_messages" in tables:
            raw_rows = [dict(r) for r in db.execute("SELECT * FROM tg_raw_messages")]
            if raw_rows:
                rcols = list(raw_rows[0])
                r_sql = (
                    f"INSERT INTO tg_raw_messages({','.join(rcols)}) VALUES ({','.join('?' for _ in rcols)}) "
                    "ON CONFLICT(post_key) DO UPDATE SET "
                    + ",".join(f"{c}=excluded.{c}" for c in rcols if c != "post_key")
                    + " WHERE tg_raw_messages.content_revision_hash != excluded.content_revision_hash"
                )
                store.execute_batch([(r_sql, tuple(rr[c] for c in rcols)) for rr in raw_rows])
                obs_sql = (
                    "UPDATE tg_raw_messages SET last_observed_at=? "
                    "WHERE post_key=? AND content_revision_hash=? AND last_observed_at<?"
                )
                store.execute_batch(
                    [
                        (
                            obs_sql,
                            (
                                rr["last_observed_at"],
                                rr["post_key"],
                                rr["content_revision_hash"],
                                rr["last_observed_at"],
                            ),
                        )
                        for rr in raw_rows
                    ]
                )
                stats["raw_synced"] = len(raw_rows)
        if "tg_cleaning_queue" in tables:
            q_rows = [dict(r) for r in db.execute("SELECT * FROM tg_cleaning_queue WHERE status='pending'")]
            if q_rows:
                qcols = list(q_rows[0])
                q_sql = (
                    f"INSERT INTO tg_cleaning_queue({','.join(qcols)}) VALUES ({','.join('?' for _ in qcols)}) "
                    "ON CONFLICT(post_key) DO UPDATE SET "
                    "content_revision_hash=excluded.content_revision_hash,"
                    "shared_body_hash=excluded.shared_body_hash,"
                    "status='pending',attempts=0,next_retry_at=0,last_error=NULL,updated_at=excluded.updated_at "
                    "WHERE tg_cleaning_queue.content_revision_hash != excluded.content_revision_hash"
                )
                store.execute_batch([(q_sql, tuple(qr[c] for c in qcols)) for qr in q_rows])
                stats["queue_synced"] = len(q_rows)
    return stats


def migrate_existing_state_to_v2(
    tg_state_dir: str | Path,
    *,
    backup_before_migrate: bool = True,
    now: float | None = None,
) -> dict[str, Any]:
    """Smoothly migrate the 67k historical records into v2 EvidenceStore.

    - Creates an in-place backup of clean/search-v3/search.sqlite before mutating.
    - Recovers full message text & attachments from tg_search_records, JSONL logs, and tg_http_cache.
    - Historical records without a v2 clean decision are marked process_status='uncleaned_backlog'
      so they do not flood the real-time incremental queue until the E5 classifier is ready.
    - Seeds tg_publish_ledger with currently accepted/published records as the baseline.
    """
    root = Path(tg_state_dir)
    now_ts = time.time() if now is None else float(now)
    clean_db_path = root / "clean" / "search-v3" / "search.sqlite"
    if not clean_db_path.is_file():
        raise FileNotFoundError(f"Cannot find existing TG database at {clean_db_path}")

    marker_path = root / "v2_evidence_migrated.json"
    if marker_path.is_file():
        return json.loads(marker_path.read_text(encoding="utf-8"))

    if backup_before_migrate:
        backup_dir = root / "backups" / f"pre-v2-{int(now_ts)}"
        backup_dir.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(f"file:{clean_db_path.resolve()}?mode=ro", uri=True, timeout=30.0) as src:
            with sqlite3.connect(backup_dir / "clean-search.sqlite") as dst:
                src.backup(dst)

    # Collect all rows across clean + 4 workers
    db_sources = [clean_db_path] + [
        root / "workers" / str(i) / "search-v3" / "search.sqlite"
        for i in range(4)
        if (root / "workers" / str(i) / "search-v3" / "search.sqlite").is_file()
    ]

    # Recover observed_text from ledger, JSONL logs, and tg_http_cache
    posts: dict[tuple[str, int], dict[str, Any]] = {}
    accepted_packages: dict[str, dict[str, Any]] = {}

    for src_path in db_sources:
        with sqlite3.connect(f"file:{src_path.resolve()}?mode=ro", uri=True, timeout=30.0) as conn:
            conn.row_factory = sqlite3.Row
            for row in conn.execute("SELECT * FROM tg_search_records"):
                payload = json.loads(row["payload"])
                evidence = json.loads(row["evidence"])
                ch = str(payload.get("channel_username") or row["channel"]).lower().lstrip("@")
                mid = int(payload.get("msg_id") or row["msg_id"])
                key = (ch, mid)
                text = evidence.get("observed_text") or ""
                entry = posts.get(key)
                if entry is None or (not entry.get("text") and text):
                    posts[key] = {
                        "channel_username": ch,
                        "msg_id": mid,
                        "tg_post_url": payload.get("tg_post_url") or f"https://t.me/{ch}/{mid}",
                        "text": text,
                        "published_timestamp": payload.get("published_timestamp"),
                        "file_attachment_name": payload.get("file_attachment_name"),
                        "has_tg_attachment": bool(payload.get("has_tg_attachment")),
                        "sender_author_signature": payload.get("sender_author_signature"),
                        "has_forward_header": payload.get("origin_type") == "native_forward",
                        "forwarded_from_url": (
                            payload.get("original_post_url")
                            if payload.get("origin_type") == "native_forward"
                            else None
                        ),
                        "links": re.findall(r"https?://[^\s<>]+", text) if text else [],
                        "link_items": [],
                        "updated": float(row["updated"] or now_ts),
                    }
                if int(row["accepted"]) == 1:
                    device = str(payload.get("device_codename") or row["device"]).lower()
                    res_key = f"{device}|{payload.get('tg_post_url') or f'https://t.me/{ch}/{mid}'}"
                    accepted_packages[res_key] = payload

    # Recover missing texts from JSONL logs if any
    log_dirs = [root / "clean" / "search-v3" / "logs", root / "legacy" / "search-v3" / "logs"] + [
        root / "workers" / str(i) / "search-v3" / "logs" for i in range(4)
    ]
    seen_logs: set[Path] = set()
    for ldir in log_dirs:
        if not ldir.is_dir():
            continue
        for lpath in sorted(ldir.glob("*.jsonl")):
            real = lpath.resolve()
            if real in seen_logs:
                continue
            seen_logs.add(real)
            try:
                with real.open("r", encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        if '"resource_decision"' not in line:
                            continue
                        try:
                            ev = json.loads(line)
                        except ValueError:
                            continue
                        if ev.get("event") != "resource_decision":
                            continue
                        obs = ev.get("observed_text")
                        pkg = ev.get("package") or {}
                        ch = str(pkg.get("channel_username") or "").lower().lstrip("@")
                        mid = pkg.get("msg_id")
                        if ch and isinstance(mid, int) and isinstance(obs, str) and obs:
                            if (ch, mid) in posts and not posts[(ch, mid)].get("text"):
                                posts[(ch, mid)]["text"] = obs
                                posts[(ch, mid)]["links"] = list(
                                    dict.fromkeys(re.findall(r"https?://[^\s<>]+", obs))
                                )
            except OSError:
                continue

    # Recover remaining missing texts from tg_http_cache in clean_db_path
    missing_keys = {k for k, v in posts.items() if not v.get("text")}
    if missing_keys:
        with sqlite3.connect(f"file:{clean_db_path.resolve()}?mode=ro", uri=True, timeout=30.0) as conn:
            has_cache = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tg_http_cache'"
            ).fetchone()
            if has_cache:
                want_channels = {ch for ch, _ in missing_keys}
                for (url, body) in conn.execute("SELECT url, body FROM tg_http_cache ORDER BY url"):
                    p = urlsplit(url)
                    m = re.fullmatch(r"/(?:s/)?([a-z][a-z0-9_]{3,31})(?:/([0-9]+))?", p.path)
                    if p.hostname != "t.me" or not m or m[1] not in want_channels:
                        continue
                    try:
                        html = zlib.decompress(body).decode("utf-8", errors="replace")
                        if m[2]:
                            html = '<section class="tgme_widget_history">' + html + "</section>"
                        for msg in parse_tg_channel_page(html, m[1])["messages"]:
                            k = (msg["channel_username"], msg["msg_id"])
                            if k in missing_keys:
                                posts[k].update(msg)
                                missing_keys.remove(k)
                    except (ValueError, UnicodeError, zlib.error, ChannelUnavailableError):
                        continue
                    if not missing_keys:
                        break

    # Write into EvidenceStore on clean/search-v3/search.sqlite
    from .quality_audit import resource_token

    store = EvidenceStore(clean_db_path)
    try:
        store.execute("BEGIN IMMEDIATE")
        try:
            migrated_posts = 0
            for (ch, mid), msg in posts.items():
                post_key = f"{ch}|{mid}"
                rev_hash = compute_content_revision_hash(msg)
                body_hash = compute_shared_body_hash(msg)
                obs_ts = float(msg.get("updated") or now_ts)
                store.connection.execute(
                    """
                    INSERT OR IGNORE INTO tg_raw_messages (
                        post_key, channel_id, channel_username, msg_id, tg_post_url,
                        published_timestamp, first_observed_at, last_observed_at,
                        raw_text, attachment_names_json, link_items_json,
                        reply_context_json, forward_context_json, media_group_context_json,
                        sender_context_json, content_revision_hash, shared_body_hash,
                        content_revision_no, process_status, process_version,
                        process_reason, is_blindspot_sample
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'uncleaned_backlog', ?, 'historical_v1_backlog', 0)
                    """,
                    (
                        post_key,
                        str(msg.get("channel_id") or f"@{ch}"),
                        ch,
                        mid,
                        msg["tg_post_url"],
                        msg.get("published_timestamp"),
                        obs_ts,
                        obs_ts,
                        msg.get("text") or "",
                        json.dumps(_canonical_attachments(msg), ensure_ascii=False),
                        json.dumps(_canonical_link_items(msg), ensure_ascii=False),
                        json.dumps(
                            {
                                "reply_to_post_url": msg.get("reply_to_post_url"),
                                "reply_to_msg_id": msg.get("reply_to_msg_id"),
                                "reply_author": msg.get("reply_author"),
                                "reply_text": msg.get("reply_text"),
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        json.dumps(
                            {
                                "has_forward_header": bool(msg.get("has_forward_header")),
                                "forwarded_from_url": msg.get("forwarded_from_url"),
                                "forwarded_from_name": msg.get("forwarded_from_name"),
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        json.dumps({"media_group_id": msg.get("media_group_id")}, ensure_ascii=False),
                        json.dumps(
                            {
                                "sender_author_signature": msg.get("sender_author_signature"),
                                "sender_username": msg.get("sender_username"),
                                "sender_id": msg.get("sender_id"),
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        rev_hash,
                        body_hash,
                        EVIDENCE_SCHEMA_VERSION,
                    ),
                )
                migrated_posts += 1

            baselined_ledger = 0
            for res_key, pkg in accepted_packages.items():
                token = resource_token(pkg)
                p_hash = hashlib.sha256(
                    json.dumps(pkg, ensure_ascii=False, sort_keys=True).encode("utf-8")
                ).hexdigest()
                store.connection.execute(
                    """
                    INSERT OR IGNORE INTO tg_publish_ledger (
                        resource_key, public_token, device_codename, target_post_url,
                        revision_id, payload_hash, status, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?)
                    """,
                    (
                        res_key,
                        token,
                        pkg["device_codename"],
                        pkg["tg_post_url"],
                        f"baseline-v1-{p_hash[:16]}",
                        p_hash,
                        now_ts,
                    ),
                )
                baselined_ledger += 1

            store.execute("COMMIT")
        except BaseException:
            store.execute("ROLLBACK")
            raise
    finally:
        store.close()

    summary = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "migrated_at": now_ts,
        "unique_posts_migrated": migrated_posts,
        "uncleaned_backlog_marked": migrated_posts,
        "posts_with_recovered_text": sum(1 for v in posts.values() if v.get("text")),
        "baselined_publish_ledger_entries": baselined_ledger,
    }
    marker_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary
