"""One-time reconciliation of real D1 live rows and v1 accepted=1 records.

1. Syncs actual D1 `custom_resources` rows into `tg_publish_ledger`, replacing the
   inflated `baseline-v1-*` entries so only truly live D1 items are marked active.
2. Skips `uncleaned_backlog` posts already live on D1 without calling DeepSeek.
3. Audits and deduplicates the remaining v1 `accepted=1` records in `tg_search_records`
   using current v2 quality/policy rules without calling DeepSeek:
   - Passing items are enqueued to `tg_publish_outbox` and marked `v1_audited_publishable`.
   - Failing/duplicate items are NOT published and marked `v1_audited_rejected`.
4. Leaves all remaining `uncleaned_backlog` posts (never accepted in v1 / scraped-only)
   to run through the full v2 pipeline (Layer 1 -> E5 -> DeepSeek -> Layer 3).
"""
from __future__ import annotations

from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
from typing import Any

from .attributed_extractor import DeviceLookupIndex, PUBLISHABLE_RESOURCE_KINDS
from .evidence_store import EvidenceStore
from .trial import atomic_json
from .explicit_publisher import compute_resource_key, enqueue_publish_operation
from .official_registry import OfficialSourcesRegistry
from .quality_audit import DB_ID, quality_errors, resource_token


def _fetch_d1_live_rows() -> list[dict[str, Any]] | None:
    account = os.environ.get("CF_ACCOUNT_ID", "").strip()
    db_id = os.environ.get("CF_D1_DATABASE_ID", "").strip()
    token = os.environ.get("CF_API_TOKEN", "").strip()
    if not (account and db_id and token):
        return None
    if db_id != DB_ID:
        raise ValueError("Production D1 identity must match the established database")
    from fmr_core.d1_client import D1Client

    db = D1Client(cf_database_id=DB_ID)
    return list(
        db.query(
            "SELECT public_token, device_codename, target_post_url, maintainer_opted_out FROM custom_resources"
        )
    )


def _sync_real_d1_to_ledger(
    store: EvidenceStore,
    d1_rows: list[dict[str, Any]],
    *,
    now_ts: float,
) -> dict[str, int]:
    stmts: list[tuple[str, tuple[Any, ...]]] = [
        ("DELETE FROM tg_publish_ledger WHERE revision_id LIKE 'baseline-v1-%'", ())
    ]
    seen_keys: set[str] = set()
    synced = 0
    for row in d1_rows:
        code = str(row.get("device_codename") or "").strip().lower()
        url = str(row.get("target_post_url") or "").strip()
        tok = str(row.get("public_token") or "").strip()
        if not code or not url:
            continue
        res_key = compute_resource_key(code, url)
        if res_key in seen_keys:
            continue
        seen_keys.add(res_key)
        status = "opted_out" if row.get("maintainer_opted_out") else "active"
        stmts.append(
            (
                """
                INSERT INTO tg_publish_ledger (
                    resource_key, public_token, device_codename, target_post_url,
                    revision_id, payload_hash, status, updated_at
                ) VALUES (?, ?, ?, ?, 'd1-live-baseline', '', ?, ?)
                ON CONFLICT(resource_key) DO UPDATE SET
                    public_token=excluded.public_token,
                    status=CASE
                        WHEN tg_publish_ledger.revision_id='d1-live-baseline' THEN excluded.status
                        ELSE tg_publish_ledger.status
                    END,
                    updated_at=excluded.updated_at
                """,
                (res_key, tok, code, url, status, now_ts),
            )
        )
        synced += 1
    store.execute_batch(stmts)
    return {"d1_live_synced": synced}


def _collect_v1_accepted_rows(root: Path) -> list[dict[str, Any]]:
    db_paths = [root / "clean" / "search-v3" / "search.sqlite"] + [
        root / "workers" / str(i) / "search-v3" / "search.sqlite" for i in range(4)
    ]
    collected: list[dict[str, Any]] = []
    seen_identity: set[tuple[str, str, int]] = set()
    for p in db_paths:
        if not p.is_file():
            continue
        with sqlite3.connect(f"file:{p.resolve()}?mode=ro", uri=True, timeout=30.0) as conn:
            conn.row_factory = sqlite3.Row
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "tg_search_records" not in tables:
                continue
            for row in conn.execute("SELECT * FROM tg_search_records WHERE accepted=1 ORDER BY updated DESC"):
                try:
                    pkg = json.loads(row["payload"])
                    ev = json.loads(row["evidence"])
                except Exception:
                    continue
                ch = str(pkg.get("channel_username") or row["channel"] or "").strip().lower().lstrip("@")
                mid = int(pkg.get("msg_id") or row["msg_id"] or 0)
                dev = str(pkg.get("device_codename") or row["device"] or "").strip().lower()
                if not ch or mid <= 0:
                    continue
                ident = (dev, ch, mid)
                if ident in seen_identity:
                    continue
                seen_identity.add(ident)
                collected.append(
                    {
                        "channel": ch,
                        "msg_id": mid,
                        "post_key": f"{ch}|{mid}",
                        "device": dev,
                        "fingerprint": str(row["fingerprint"] or ""),
                        "package": pkg,
                        "evidence": ev,
                    }
                )
    return collected


def reconcile_v1_backlog(
    tg_state_dir: str | Path,
    store: EvidenceStore,
    *,
    names: dict[str, list[str]],
    registry: OfficialSourcesRegistry,
    lookup: DeviceLookupIndex,
    d1_rows: list[dict[str, Any]] | None = None,
    process_version: str = "tg-v2-cleaner-v1.0",
    now: float | None = None,
) -> dict[str, Any]:
    """Run one-time D1 ledger sync + zero-LLM audit of v1 accepted=1 records."""
    root = Path(tg_state_dir)
    marker_path = root / "v2_v1_backlog_audited.json"
    if marker_path.is_file():
        return {"status": "already_reconciled"}

    now_ts = time.time() if now is None else float(now)
    live_rows = d1_rows if d1_rows is not None else _fetch_d1_live_rows()
    if live_rows is None:
        return {"status": "skipped_no_d1_credentials"}

    sync_info = _sync_real_d1_to_ledger(store, live_rows, now_ts=now_ts)
    skipped_d1_backlog = store.skip_active_ledger_backlog(process_version=process_version)

    live_urls = {
        str(r["target_post_url"]).strip()
        for r in store.query(
            "SELECT target_post_url FROM tg_publish_ledger WHERE status IN ('active', 'opted_out')"
        )
        if r.get("target_post_url")
    }
    live_tokens = {
        str(r["public_token"]).strip()
        for r in store.query(
            "SELECT public_token FROM tg_publish_ledger WHERE status IN ('active', 'opted_out') AND public_token != ''"
        )
    }

    v1_rows = _collect_v1_accepted_rows(root)
    counts: Counter[str] = Counter()
    counts["v1_accepted_total"] = len(v1_rows)
    counts["d1_live_synced"] = sync_info["d1_live_synced"]
    counts["backlog_skipped_d1_live"] = skipped_d1_backlog

    # Phase 1: Filter & deduplicate v1 accepted=1 items using current v2 audit rules
    dedup_by_fp: dict[str, dict[str, Any]] = {}
    rejected_posts: dict[str, str] = {}
    skipped_d1_posts: set[str] = set()

    for rec in v1_rows:
        pkg = dict(rec["package"])
        post_key = rec["post_key"]
        url = str(pkg.get("tg_post_url") or f"https://t.me/{rec['channel']}/{rec['msg_id']}").strip()
        pkg["tg_post_url"] = url

        tok = resource_token(pkg)
        if url in live_urls or tok in live_tokens:
            counts["v1_skipped_already_in_d1"] += 1
            skipped_d1_posts.add(post_key)
            continue

        resolved_code = lookup.resolve_codename(rec["device"]) or lookup.resolve_codename(str(pkg.get("device_codename") or ""))
        if not resolved_code or resolved_code not in names:
            counts["v1_rejected_unresolved_device"] += 1
            rejected_posts.setdefault(post_key, "v1_audit:unresolved_device")
            continue
        pkg["device_codename"] = resolved_code

        kind = str(pkg.get("resource_kind") or "rom").strip().lower()
        if kind not in PUBLISHABLE_RESOURCE_KINDS:
            counts["v1_rejected_non_core_kind"] += 1
            rejected_posts.setdefault(post_key, f"v1_audit:non_core_kind:{kind}")
            continue

        tax = pkg.get("taxonomy") or {}
        proj = (tax.get("project") or {}).get("name") or pkg.get("project_name") or ""
        rel_status = str(pkg.get("release_type") or (tax.get("maintenance") or {}).get("release_status") or "").lower()
        is_off_excl, off_reason = registry.should_exclude_official_project(str(proj), rel_status, kind)
        if is_off_excl:
            counts["v1_rejected_official_project"] += 1
            rejected_posts.setdefault(post_key, f"v1_audit:{off_reason or 'official_project'}")
            continue

        errs = quality_errors(pkg, True)
        if errs:
            counts["v1_rejected_quality_errors"] += 1
            rejected_posts.setdefault(post_key, f"v1_audit:{errs[0]}")
            continue

        fp = rec["fingerprint"] or tok
        existing = dedup_by_fp.get(fp)
        if existing is None:
            dedup_by_fp[fp] = {**rec, "package": pkg, "token": tok}
        else:
            curr_orig = existing["package"].get("origin_type")
            new_orig = pkg.get("origin_type")
            if curr_orig != "author_direct" and new_orig == "author_direct":
                rejected_posts.setdefault(existing["post_key"], "v1_audit:dedup_superseded")
                dedup_by_fp[fp] = {**rec, "package": pkg, "token": tok}
                counts["v1_rejected_duplicate"] += 1
            else:
                rejected_posts.setdefault(post_key, "v1_audit:dedup_duplicate")
                counts["v1_rejected_duplicate"] += 1

    # Phase 2: Enqueue passing items to tg_publish_outbox & mark raw messages terminal
    passed_posts: set[str] = set()
    seen_tokens: set[str] = set(live_tokens)
    for item in dedup_by_fp.values():
        tok = item["token"]
        post_key = item["post_key"]
        if tok in seen_tokens:
            counts["v1_rejected_duplicate"] += 1
            rejected_posts.setdefault(post_key, "v1_audit:duplicate_token")
            continue
        seen_tokens.add(tok)
        pkg = item["package"]
        url = pkg["tg_post_url"]
        code = pkg["device_codename"]
        p_hash = hashlib.sha256(
            json.dumps(pkg, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        res_key = compute_resource_key(code, url)
        enq = enqueue_publish_operation(
            store,
            op_type="ADD",
            resource_key=res_key,
            revision_id=f"v1-audited-{p_hash[:16]}",
            device_codename=code,
            tg_post_url=url,
            item={"package": pkg, "evidence": item["evidence"]},
            now=now_ts,
        )
        if enq.get("enqueued"):
            counts["v1_passed_enqueued"] += 1
        else:
            counts["v1_passed_already_outboxed"] += 1
        passed_posts.add(post_key)

    # Mark tg_raw_messages so none of the 43k v1 accepted=1 posts hit DeepSeek again
    for post_key in skipped_d1_posts:
        store.mark_cleaning_result(
            post_key,
            process_status="skipped_already_published",
            process_reason="already_active_in_d1",
            process_version=process_version,
            now=now_ts,
        )
    for post_key in passed_posts:
        store.mark_cleaning_result(
            post_key,
            process_status="v1_audited_publishable",
            process_reason="v1_record_passed_v2_audit",
            process_version=process_version,
            now=now_ts,
        )
    for post_key, reason in rejected_posts.items():
        if post_key in passed_posts or post_key in skipped_d1_posts:
            continue
        store.mark_cleaning_result(
            post_key,
            process_status="v1_audited_rejected",
            process_reason=reason,
            process_version=process_version,
            now=now_ts,
        )

    summary = {
        "status": "reconciled",
        "reconciled_at": now_ts,
        "process_version": process_version,
        "counts": dict(counts),
    }
    atomic_json(marker_path, summary)
    return summary
