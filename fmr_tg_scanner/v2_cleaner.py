"""TG Pipeline v2 Incremental Cleaner (Layer 1 -> Layer 2 E5 ONNX -> Layer 3 DeepSeek & Explicit Outbox).

Consumes incremental messages from 103 SQLite `tg_cleaning_queue` (and optional bounded
`uncleaned_backlog`), performs 3-layer DB persistence, routes incomplete releases to
`tg_pending_evidence_queue`, and enqueues explicit ADD/UPDATE/WITHDRAW ops into `tg_publish_outbox`.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time
import traceback
from typing import Any

from .attributed_extractor import (
    ChannelProfileMatcher,
    DeepSeekAttributedClient,
    DeviceLookupIndex,
    EXTRACTION_SPEC_VERSION,
    TransientLLMError,
    compute_extraction_cache_key,
    materialize_publishable_items,
    normalize_and_verify_extraction,
    normalize_stored_message,
)
from .evidence_store import EvidenceStore, sync_raw_and_queue_from_snapshot
from .explicit_publisher import enqueue_publish_operation
from .intent_classifier import IntentClassifier
from .official_registry import OfficialSourcesRegistry, load_official_registry
from .runtime_status import record_business_failure, record_business_success
from .targets import read_catalogue
from .v1_reconcile import reconcile_v1_backlog
from .trial import atomic_json

V2_CLEANER_VERSION = "tg-v2-cleaner-v1.0"
DEFAULT_MODEL_DIR = Path("/var/lib/findmyrom/models/tg-intent-v2")
WORKERS = 4


def sync_workers_to_clean(root: Path, *, workers: int = WORKERS) -> dict[str, int]:
    """Copy clean SQLite snapshots (.backup) from all fetch shards into clean/search-v3/search.sqlite."""
    clean_search = root / "clean" / "search-v3"
    clean_search.mkdir(parents=True, exist_ok=True)
    clean_db = clean_search / "search.sqlite"
    snapshots_dir = root / "clean" / "snapshots"
    snapshots_dir.mkdir(parents=True, exist_ok=True)

    from .local_runtime import backup

    sources = [root / "legacy" / "search-v3" / "search.sqlite"] + [
        root / "workers" / str(i) / "search-v3" / "search.sqlite" for i in range(workers)
    ]
    existing_sources = [s for s in sources if s.is_file()]
    if not clean_db.is_file():
        if not existing_sources:
            raise FileNotFoundError(f"No fetch/clean SQLite found under {root}")
        backup(existing_sources[0], clean_db)

    total_raw = 0
    total_queue = 0
    total_profiles = 0
    store = EvidenceStore(clean_db)
    try:
        for idx, src in enumerate(sources):
            if not src.is_file():
                continue
            snap = snapshots_dir / f"v2-{idx}.sqlite"
            backup(src, snap)
            merged = store.sync_raw_and_queue_from_snapshot(snap)
            total_raw += merged["raw_synced"]
            total_queue += merged["queue_synced"]
            total_profiles += merged.get("profiles_synced", 0)
    finally:
        store.close()
    return {"raw_synced": total_raw, "queue_synced": total_queue, "profiles_synced": total_profiles}


def _find_active_ledger_targets(
    store: EvidenceStore,
    message: dict[str, Any],
) -> list[dict[str, Any]]:
    """Locate active tg_publish_ledger entries targeted by an explicit withdrawal post/reply."""
    channel = str(message.get("channel_username") or "").strip().lower().lstrip("@")
    candidate_urls = [u for u in (message.get("tg_post_url"), message.get("reply_to_post_url")) if u]
    if not candidate_urls:
        return []
    placeholders = ",".join("?" for _ in candidate_urls)
    rows = store.query(
        f"""
        SELECT resource_key, public_token, device_codename, target_post_url, revision_id, status
        FROM tg_publish_ledger
        WHERE status='active' AND target_post_url IN ({placeholders})
        """,
        candidate_urls,
    )
    prefix = f"https://t.me/{channel}/" if channel else ""
    return [r for r in rows if not prefix or str(r.get("target_post_url") or "").lower().startswith(prefix)]


def clean_v2_once(
    state_dir: str | Path,
    *,
    model_dir: str | Path | None = None,
    official_config_path: str | Path | None = None,
    catalogue_dir: str | Path | None = None,
    catalogue_names: dict[str, list[str]] | None = None,
    queue_limit: int = 100,
    backlog_limit: int = 0,
    max_llm_calls: int = 50,
    retry_cooldown_seconds: int = 900,
    sync_workers: bool = True,
    dry_run: bool = False,
    classifier: Any | None = None,
    llm_client: DeepSeekAttributedClient | None = None,
    channel_profiles: dict[str, str] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Execute one incremental TG v2 cleaning cycle and return structured audit stats."""
    root = Path(state_dir)
    now_ts = time.time() if now is None else float(now)

    sync_stats = {"raw_synced": 0, "queue_synced": 0, "profiles_synced": 0}
    if sync_workers and not dry_run:
        sync_stats = sync_workers_to_clean(root)

    clean_db = root / "clean" / "search-v3" / "search.sqlite"
    if not clean_db.is_file():
        return {"status": "waiting_for_db", "clean_db": str(clean_db)}

    if catalogue_names is None:
        cat_dir = Path(catalogue_dir) if catalogue_dir is not None else None
        names, cat_sources = read_catalogue(cat_dir)
    else:
        names = catalogue_names
        cat_sources = [{"file": "injected", "devices_read": len(names)}]
    lookup = DeviceLookupIndex(names)

    registry: OfficialSourcesRegistry = load_official_registry(official_config_path)
    clf = classifier
    if clf is None:
        m_dir = Path(model_dir or os.getenv("FMR_TG_INTENT_MODEL_DIR", str(DEFAULT_MODEL_DIR)))
        clf = IntentClassifier(model_dir=m_dir, registry=registry)

    client = llm_client or DeepSeekAttributedClient()
    store = EvidenceStore(clean_db)
    profile_matcher = ChannelProfileMatcher(lookup)
    raw_profiles = store.load_channel_profiles()
    if channel_profiles:
        raw_profiles.update({str(k).strip().lower().lstrip("@"): v for k, v in channel_profiles.items()})
    counts: Counter[str] = Counter()
    sample_decisions: list[dict[str, Any]] = []
    llm_sample_decisions: list[dict[str, Any]] = []
    token_usage: Counter[str] = Counter()

    try:
        v1_reconcile: dict[str, Any] = {"status": "skipped_dry_run"}
        if not dry_run:
            v1_reconcile = reconcile_v1_backlog(
                root,
                store,
                names=names,
                registry=registry,
                lookup=lookup,
                process_version=V2_CLEANER_VERSION,
                now=now_ts,
            )
        due_rows = store.fetch_due_cleaning_batch(limit=queue_limit, now=now_ts)
        backlog_rows = []
        remaining_slots = max(0, backlog_limit)
        if remaining_slots > 0 and len(due_rows) < queue_limit:
            backlog_rows = store.fetch_backlog_batch(limit=remaining_slots)

        batch_rows = [*due_rows, *backlog_rows]
        counts["due_queue_fetched"] = len(due_rows)
        counts["backlog_fetched"] = len(backlog_rows)

        if not batch_rows:
            report = {
                "status": "idle_no_due_items",
                "cleaner_version": V2_CLEANER_VERSION,
                "spec_version": EXTRACTION_SPEC_VERSION,
                "dry_run": dry_run,
                "sync": sync_stats,
                "catalogue_devices": len(names),
                "catalogue_sources": cat_sources,
                "counts": dict(counts),
                "token_usage": {},
                "sample_decisions": [],
                "llm_sample_decisions": [],
                "completed_at": datetime.now(timezone.utc).isoformat(),
            }
            if not dry_run:
                atomic_json(root / "v2-clean-latest.json", report)
            return report

        ready_to_materialize: list[tuple[dict[str, Any], str, dict[str, Any], bool, bool]] = []
        pending_llm: list[tuple[dict[str, Any], str, bool]] = []
        in_batch_followers: dict[str, list[tuple[dict[str, Any], bool]]] = {}

        for raw_row in batch_rows:
            msg = normalize_stored_message(raw_row)
            post_key = msg["post_key"]
            is_unedited_or_backlog = (
                raw_row.get("status") == "uncleaned_backlog"
                or int(raw_row.get("content_revision_no") or 1) <= 1
            )
            if is_unedited_or_backlog and store.is_post_url_active_in_ledger(msg.get("tg_post_url") or ""):
                counts["skipped_already_published"] += 1
                sample_decisions.append(
                    {
                        "post_key": post_key,
                        "action": "skipped_already_published",
                        "reason": "already_active_in_d1",
                    }
                )
                if not dry_run:
                    store.mark_cleaning_result(
                        post_key,
                        process_status="skipped_already_published",
                        process_reason="already_active_in_d1",
                        process_version=V2_CLEANER_VERSION,
                        now=now_ts,
                    )
                continue
            route = clf.route_message(msg)
            action = route["action"]

            if action == "skip_channel_blocked":
                counts["layer1_blocked"] += 1
                sample_decisions.append({"post_key": post_key, "action": action, "reason": route["reason"]})
                if not dry_run:
                    store.mark_cleaning_result(
                        post_key,
                        process_status="layer1_blocked",
                        process_reason=route["reason"],
                        process_version=V2_CLEANER_VERSION,
                        now=now_ts,
                    )
                continue

            if action in ("hard_filtered", "blindspot_retained"):
                counts[action] += 1
                sample_decisions.append(
                    {
                        "post_key": post_key,
                        "action": action,
                        "reason": route["reason"],
                        "blindspot_retained": action == "blindspot_retained",
                    }
                )
                if not dry_run:
                    store.mark_cleaning_result(
                        post_key,
                        process_status=action,
                        process_reason=route["reason"],
                        process_version=V2_CLEANER_VERSION,
                        is_blindspot=(action == "blindspot_retained"),
                        now=now_ts,
                    )
                continue

            if action == "skip_irrelevant":
                counts["e5_skipped_irrelevant"] += 1
                sample_decisions.append(
                    {"post_key": post_key, "action": action, "reason": route["reason"], "probs": route.get("probs")}
                )
                if not dry_run:
                    store.mark_cleaning_result(
                        post_key,
                        process_status="e5_skipped_irrelevant",
                        process_reason=route["reason"],
                        process_version=V2_CLEANER_VERSION,
                        now=now_ts,
                    )
                continue

            if action == "drop_insufficient_context":
                counts["e5_dropped_insufficient"] += 1
                sample_decisions.append(
                    {"post_key": post_key, "action": action, "reason": route["reason"], "probs": route.get("probs")}
                )
                if not dry_run:
                    store.mark_cleaning_result(
                        post_key,
                        process_status="e5_dropped_insufficient",
                        process_reason=route["reason"],
                        process_version=V2_CLEANER_VERSION,
                        now=now_ts,
                    )
                continue

            if not route.get("send_to_deepseek"):
                counts["skipped_non_llm_route"] += 1
                sample_decisions.append(
                    {"post_key": post_key, "action": action, "reason": route.get("reason", "send_to_deepseek_false")}
                )
                if not dry_run:
                    store.mark_cleaning_result(
                        post_key,
                        process_status=str(action or "skipped_non_llm_route"),
                        process_reason=str(route.get("reason") or "send_to_deepseek_false"),
                        process_version=V2_CLEANER_VERSION,
                        now=now_ts,
                    )
                continue

            # send_to_deepseek (regular release_related/ambiguous OR 3% audit_sample_to_deepseek)
            audit_sampled = bool(route.get("audit_sampled"))
            if audit_sampled:
                counts["e5_audit_sampled"] += 1
            else:
                counts["e5_passed_to_llm_stage"] += 1

            cache_key = compute_extraction_cache_key(msg, model_name=client.model_name)
            cached = store.get_extraction_ledger(cache_key)
            if cached and isinstance(cached.get("raw_model"), dict):
                counts["extraction_cache_hits"] += 1
                ready_to_materialize.append((msg, cache_key, cached["raw_model"], audit_sampled, True))
                continue

            if cache_key in in_batch_followers:
                counts["extraction_cache_hits"] += 1
                in_batch_followers[cache_key].append((msg, audit_sampled))
                continue

            if len(pending_llm) < max(0, max_llm_calls):
                pending_llm.append((msg, cache_key, audit_sampled))
                in_batch_followers[cache_key] = []
            else:
                counts["llm_budget_deferred"] += 1

        if pending_llm:
            try:
                client.ensure_configured()
            except TransientLLMError as exc:
                err = str(exc)
                if not dry_run:
                    for msg, cache_key, _ in pending_llm:
                        for target_msg in [msg, *(m for m, _ in in_batch_followers.get(cache_key, []))]:
                            if store.query("SELECT 1 FROM tg_cleaning_queue WHERE post_key=?", (target_msg["post_key"],)):
                                store.mark_cleaning_retry(
                                    target_msg["post_key"],
                                    err,
                                    cooldown_seconds=retry_cooldown_seconds,
                                    now=now_ts,
                                )
                raise TransientLLMError(f"LLM unavailable for pending batch ({len(pending_llm)} items): {err}") from exc

            llm_results = client.extract_batch([msg for msg, _, _ in pending_llm])
            by_key = {msg["post_key"]: (msg, cache_key, audit_sampled) for msg, cache_key, audit_sampled in pending_llm}
            ok_in_batch = 0
            last_err: str | None = None

            for res in llm_results:
                counts["llm_calls_attempted"] += 1
                post_key = res["post_key"]
                msg, cache_key, audit_sampled = by_key[post_key]
                followers = in_batch_followers.get(cache_key, [])
                for metric in ("prompt_cache_hit_tokens", "prompt_cache_miss_tokens", "completion_tokens", "total_tokens"):
                    val = (res.get("usage") or {}).get(metric)
                    if type(val) is int and val >= 0:
                        token_usage[metric] += val
                if res["ok"] and isinstance(res.get("raw_model"), dict):
                    ok_in_batch += 1
                    counts["llm_calls_succeeded"] += 1
                    ready_to_materialize.append((msg, cache_key, res["raw_model"], audit_sampled, False))
                    for follower_msg, follower_audit in followers:
                        ready_to_materialize.append((follower_msg, cache_key, res["raw_model"], follower_audit, True))
                else:
                    counts["llm_calls_failed_retry_wait"] += 1 + len(followers)
                    last_err = str(res.get("error") or "llm_transient_failure")
                    for target_msg in [msg, *(m for m, _ in followers)]:
                        pk = target_msg["post_key"]
                        if not dry_run and store.query("SELECT 1 FROM tg_cleaning_queue WHERE post_key=?", (pk,)):
                            store.mark_cleaning_retry(
                                pk,
                                last_err,
                                cooldown_seconds=retry_cooldown_seconds,
                                now=now_ts,
                            )
                        sample_decisions.append({"post_key": pk, "action": "retry_wait", "error": last_err})

            if ok_in_batch == 0 and last_err is not None:
                raise TransientLLMError(f"All {len(pending_llm)} DeepSeek extraction calls failed: {last_err}")

        for msg, cache_key, raw_model, audit_sampled, from_cache in ready_to_materialize:
            post_key = msg["post_key"]
            normalized = normalize_and_verify_extraction(raw_model, msg, registry=registry)
            if audit_sampled and normalized["is_release"] and normalized["decision"] == "eligible":
                counts["audit_sample_false_negative_caught"] += 1

            # Explicit Withdrawal Handling (§3.5 / §3.6)
            if normalized["decision"] == "withdraw":
                targets = _find_active_ledger_targets(store, msg)
                withdrawn_keys: list[str] = []
                for t in targets:
                    withdrawn_keys.append(t["resource_key"])
                    if not dry_run:
                        res = enqueue_publish_operation(
                            store,
                            op_type="WITHDRAW",
                            resource_key=t["resource_key"],
                            revision_id=f"{(msg['content_revision_hash'] or 'wd')[:16]}-withdraw",
                            device_codename=t["device_codename"],
                            tg_post_url=t["target_post_url"],
                            withdraw_reason=normalized.get("subject_summary") or "explicit_withdrawal_post",
                            now=now_ts,
                        )
                        if res.get("enqueued"):
                            counts["outbox_withdrawn"] += 1
                counts["withdrawals_processed"] += 1
                published_summary = {
                    "status": "withdrawn",
                    "withdrawn_resource_keys": withdrawn_keys,
                    "from_cache": from_cache,
                }
                if not dry_run:
                    store.put_extraction_ledger(
                        extraction_cache_key=cache_key,
                        shared_body_hash=msg.get("shared_body_hash") or "",
                        model_name=client.model_name,
                        spec_version=EXTRACTION_SPEC_VERSION,
                        raw_model=raw_model,
                        normalized=normalized,
                        published=published_summary,
                        now=now_ts,
                    )
                    store.clear_pending_evidence(post_key)
                    store.mark_cleaning_result(
                        post_key,
                        process_status="withdrawn",
                        process_reason=f"explicit_withdraw:{len(withdrawn_keys)}",
                        process_version=V2_CLEANER_VERSION,
                        now=now_ts,
                    )
                wd_entry = {
                    "post_key": post_key,
                    "channel_username": msg["channel_username"],
                    "tg_post_url": msg["tg_post_url"],
                    "action": "withdraw",
                    "withdrawn_keys": withdrawn_keys,
                    "subject_summary": normalized["subject_summary"],
                    "raw_text_snippet": (msg["raw_text"] or "")[:240],
                    "from_cache": from_cache,
                }
                sample_decisions.append(wd_entry)
                if len(llm_sample_decisions) < 50:
                    llm_sample_decisions.append(wd_entry)
                continue

            profile_device = profile_matcher.resolve_single_device(
                raw_profiles.get(msg["channel_username"])
            )
            mat = materialize_publishable_items(
                msg,
                normalized,
                lookup,
                channel_profile_device=profile_device,
            )
            if mat.get("device_resolution", {}).get("channel_profile_fallback"):
                counts["channel_profile_fallback_used"] += 1
            published_summary = {
                "status": mat["status"],
                "reason": mat["reason"],
                "publishable_keys": [p["resource_key"] for p in mat["publishable"]],
                "pending_evidence": mat["pending_evidence"],
                "from_cache": from_cache,
            }
            if not dry_run:
                store.put_extraction_ledger(
                    extraction_cache_key=cache_key,
                    shared_body_hash=msg.get("shared_body_hash") or "",
                    model_name=client.model_name,
                    spec_version=EXTRACTION_SPEC_VERSION,
                    raw_model=raw_model,
                    normalized=normalized,
                    published=published_summary,
                    now=now_ts,
                )

            if mat["publishable"]:
                counts["posts_publishable"] += 1
                for pub in mat["publishable"]:
                    if dry_run:
                        counts["outbox_would_enqueue"] += 1
                    else:
                        enq = enqueue_publish_operation(
                            store,
                            op_type="ADD",
                            resource_key=pub["resource_key"],
                            revision_id=pub["revision_id"],
                            device_codename=pub["device_codename"],
                            tg_post_url=pub["tg_post_url"],
                            item=pub["item"],
                            now=now_ts,
                        )
                        if enq.get("enqueued"):
                            if enq["op_type"] == "UPDATE":
                                counts["outbox_updated"] += 1
                            else:
                                counts["outbox_added"] += 1
                        else:
                            counts[f"outbox_skipped_{enq.get('reason', 'unknown')}"] += 1
                if not dry_run:
                    store.clear_pending_evidence(post_key)
                    store.mark_cleaning_result(
                        post_key,
                        process_status="published_outbox",
                        process_reason=mat["reason"],
                        process_version=V2_CLEANER_VERSION,
                        now=now_ts,
                    )
            elif mat["status"] == "pending_evidence":
                counts["pending_evidence_enqueued"] += 1
                first_pe = mat["pending_evidence"][0]
                all_missing = sorted({err for pe in mat["pending_evidence"] for err in pe["missing_fields"]})
                if not dry_run:
                    store.upsert_pending_evidence(
                        post_key,
                        device_codename=first_pe["device_codename"],
                        project_name=first_pe["project_name"],
                        missing_fields=all_missing,
                        now=now_ts,
                    )
                    store.mark_cleaning_result(
                        post_key,
                        process_status="pending_evidence",
                        process_reason=mat["reason"],
                        process_version=V2_CLEANER_VERSION,
                        now=now_ts,
                    )
            else:
                counts[f"layer3_{mat['status']}"] += 1
                if not dry_run:
                    store.clear_pending_evidence(post_key)
                    store.mark_cleaning_result(
                        post_key,
                        process_status=mat["status"],
                        process_reason=mat["reason"],
                        process_version=V2_CLEANER_VERSION,
                        now=now_ts,
                    )

            llm_entry = {
                "post_key": post_key,
                "channel_username": msg["channel_username"],
                "tg_post_url": msg["tg_post_url"],
                "action": mat["status"],
                "reason": mat["reason"],
                "subject_summary": normalized["subject_summary"],
                "resource_kind": normalized["resource_kind"],
                "project_name": normalized["project_name"]["value"],
                "release_status": normalized["release_status"]["value"],
                "project_version": normalized["project_version"]["value"],
                "android_version": normalized["android_version"]["value"],
                "build_date": normalized["build_date"],
                "resolved_devices": (mat.get("device_resolution") or {}).get("resolved_codenames", []),
                "channel_profile_fallback": bool((mat.get("device_resolution") or {}).get("channel_profile_fallback")),
                "publishable_keys": published_summary["publishable_keys"],
                "missing_fields": [pe["missing_fields"] for pe in mat["pending_evidence"]],
                "raw_text_snippet": (msg["raw_text"] or "")[:240],
                "from_cache": from_cache,
                "audit_sampled": audit_sampled,
            }
            sample_decisions.append(llm_entry)
            if (not from_cache) or len(llm_sample_decisions) < 50:
                if not from_cache:
                    llm_sample_decisions.insert(0, llm_entry)
                    llm_sample_decisions = llm_sample_decisions[:50]
                else:
                    llm_sample_decisions.append(llm_entry)

        outbox_new_ops = counts["outbox_added"] + counts["outbox_updated"] + counts["outbox_withdrawn"]
        report = {
            "status": "outbox_enqueued" if outbox_new_ops > 0 else "cleaned_no_new_outbox",
            "cleaner_version": V2_CLEANER_VERSION,
            "spec_version": EXTRACTION_SPEC_VERSION,
            "dry_run": dry_run,
            "sync": sync_stats,
            "v1_reconcile": v1_reconcile,
            "catalogue_devices": len(names),
            "catalogue_sources": cat_sources,
            "outbox_new_operations": outbox_new_ops,
            "counts": dict(counts),
            "token_usage": dict(token_usage),
            "sample_decisions": sample_decisions[:30],
            "llm_sample_decisions": llm_sample_decisions[:50],
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        if not dry_run:
            atomic_json(root / "v2-clean-latest.json", report)
        return report
    finally:
        store.close()


def run_v2_loop(
    state_dir: str | Path,
    *,
    duration: int = 600,
    period: int = 600,
    queue_limit: int = 100,
    backlog_limit: int = 0,
    max_llm_calls: int = 50,
    model_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Production incremental loop using clean.lock and honest runtime status logging."""
    from .local_runtime import dispatch, lock

    root = Path(state_dir)
    last_report: dict[str, Any] = {"status": "waiting_for_migration"}
    with lock(root, "clean"):
        deadline = time.monotonic() + duration
        cycle = 0
        while time.monotonic() < deadline:
            started = time.monotonic()
            try:
                if not (root / "initialized.json").exists():
                    last_report = {"status": "waiting_for_migration"}
                else:
                    last_report = clean_v2_once(
                        root,
                        model_dir=model_dir,
                        queue_limit=queue_limit,
                        backlog_limit=backlog_limit,
                        max_llm_calls=max_llm_calls,
                    )
                    has_new = int(last_report.get("outbox_new_operations") or 0) > 0
                    record_business_success(
                        root,
                        "clean",
                        worker=None,
                        has_new_items=has_new,
                        summary={
                            "status": last_report.get("status"),
                            "outbox_new_operations": int(last_report.get("outbox_new_operations") or 0),
                            "counts": last_report.get("counts"),
                        },
                    )
                    if has_new:
                        dispatch("catalogue_publish.yml", {"scope": "tg"})
                print(
                    json.dumps(
                        {
                            "event": "local_TG_v2_clean_cycle",
                            "cycle": cycle,
                            "status": last_report.get("status"),
                            "outbox_new_operations": last_report.get("outbox_new_operations", 0),
                            "counts": last_report.get("counts", {}),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
            except Exception as exc:
                traceback.print_exc(limit=6)
                record_business_failure(root, "clean", exc, worker=None)
                atomic_json(
                    root / "clean-0-error.json",
                    {"at": time.time(), "error": type(exc).__name__, "message": str(exc)[:300]},
                )
                print(
                    json.dumps(
                        {"event": "local_TG_v2_clean_error", "error": type(exc).__name__, "message": str(exc)[:200]},
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                raise SystemExit(1) from exc
            cycle += 1
            target = min(deadline, started + (30 if not (root / "initialized.json").exists() else period))
            while time.monotonic() < target:
                time.sleep(min(30, max(0, target - time.monotonic())))
                print(json.dumps({"event": "local_TG_v2_clean_heartbeat", "cycle": cycle}), flush=True)
    return last_report


def main() -> None:
    parser = argparse.ArgumentParser(description="TG Pipeline v2 incremental cleaner")
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--model-dir", default=os.getenv("FMR_TG_INTENT_MODEL_DIR", str(DEFAULT_MODEL_DIR)))
    parser.add_argument("--catalogue-dir", default=None)
    parser.add_argument("--queue-limit", type=int, default=int(os.getenv("FMR_TG_V2_QUEUE_LIMIT", "100")))
    parser.add_argument("--backlog-limit", type=int, default=int(os.getenv("FMR_TG_V2_BACKLOG_LIMIT", "0")))
    parser.add_argument("--max-llm-calls", type=int, default=int(os.getenv("FMR_TG_CLEAN_MAX_CALLS", "50")))
    parser.add_argument("--once", action="store_true", help="Run a single cycle without looping")
    parser.add_argument("--dry-run", action="store_true", help="Evaluate without mutating DB or enqueueing outbox")
    parser.add_argument("--no-sync", action="store_true", help="Skip syncing worker shards before running")
    parser.add_argument("--duration", type=int, default=600)
    args = parser.parse_args()

    os.environ["FMR_TG_LOCAL_RUNTIME"] = "true"
    root = Path(args.state_dir).resolve()
    if args.once or args.dry_run:
        res = clean_v2_once(
            root,
            model_dir=args.model_dir,
            catalogue_dir=args.catalogue_dir,
            queue_limit=args.queue_limit,
            backlog_limit=args.backlog_limit,
            max_llm_calls=args.max_llm_calls,
            sync_workers=not args.no_sync,
            dry_run=args.dry_run,
        )
        print(json.dumps(res, ensure_ascii=False, indent=2), flush=True)
    else:
        run_v2_loop(
            root,
            duration=args.duration,
            queue_limit=args.queue_limit,
            backlog_limit=args.backlog_limit,
            max_llm_calls=args.max_llm_calls,
            model_dir=args.model_dir,
        )


if __name__ == "__main__":
    main()
