"""Stratified sampler for TG v2 intent classification annotation datasets.

Exports disjoint (channel & time split) datasets from 103's historical TG records:
  - train_candidates_2000.jsonl (1,500~2,000 samples for offline strong-model distillation)
  - golden_val_200.jsonl (150~200 samples for manual gold-standard labeling & threshold T calibration)

Strictly follows Question-1-only labeling ("is the author releasing/updating/fixing/withdrawing
a resource?"), independent of Question 3 (site admission policy).
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import random
import re
import sqlite3
from typing import Any
from urllib.parse import urlsplit
import zlib

from .evidence_store import compute_shared_body_hash
from .html_parser import ChannelUnavailableError, parse_tg_channel_page

REQUEST_OR_QUESTION_RE = re.compile(
    r"[?？]|\b(?:anyone|anybody|pls|please|help|how\s+to|where\s+(?:is|can)|"
    r"eta\b|bug|issue|problem|battery|drain|bootloop|need\s+link|send\s+link|fix\s+link)\b|"
    r"求包|求链接|有没有|怎么刷|能用吗|哪里下|什么时候",
    re.I,
)
TEASER_RE = re.compile(
    r"\b(?:soon|coming\s+soon|stay\s+tuned|teaser|sneak\s+peek|wip|work\s+in\s+progress)\b|"
    r"即将|敬请期待|预告|测试中",
    re.I,
)
NON_EN_RE = re.compile(r"[\u4e00-\u9fff\u0400-\u04ff\u0600-\u06ff]")


def format_classifier_input(
    raw_text: str,
    attachment_names: list[str] | None = None,
    link_items: list[dict[str, str]] | None = None,
    reply_context: dict[str, Any] | None = None,
) -> str:
    """Build the exact concatenated input expected by the multilingual-e5-small 3-class model."""
    parts = [f"passage: {(raw_text or '').strip()}"]
    attachments = [str(a).strip() for a in (attachment_names or []) if str(a).strip()]
    if attachments:
        parts.append("[ATTACHMENTS] " + " | ".join(attachments))
    link_labels = [
        f"{item.get('label', '').strip()} ({item.get('url', '').strip()})".strip()
        for item in (link_items or [])
        if isinstance(item, dict) and (item.get("label") or item.get("url"))
    ]
    if link_labels:
        parts.append("[LINKS] " + " | ".join(link_labels[:10]))
    reply = reply_context or {}
    if reply.get("reply_text") or reply.get("reply_to_post_url"):
        r_snippet = f"{reply.get('reply_author') or ''} {reply.get('reply_text') or ''}".strip()
        parts.append(f"[REPLY] {r_snippet or reply.get('reply_to_post_url')}")
    return "\n".join(p for p in parts if p).strip()


def classify_stratum(record: dict[str, Any]) -> str:
    text = record.get("raw_text") or ""
    attachments = record.get("attachment_names") or []
    links = record.get("link_items") or []
    has_media_or_link = bool(attachments or links)

    if len(text.strip()) < 140 and has_media_or_link:
        return "short_with_attachment_or_link"
    if REQUEST_OR_QUESTION_RE.search(text):
        return "hard_request_or_question"
    if TEASER_RE.search(text):
        return "hard_teaser_or_wip"
    if len(text.strip()) < 120:
        return "short_text_only"
    if NON_EN_RE.search(text):
        return "non_english_multilingual"
    if record.get("b01_failed") or record.get("legacy_accepted") is False:
        return "rejected_or_b01fail"
    if len(text.strip()) >= 500:
        return "long_changelog_or_release"
    return "standard_post"


def load_candidate_pool(tg_state_dir: str | Path) -> list[dict[str, Any]]:
    """Recover unique raw posts with full text & legacy metadata from 103 state directory."""
    root = Path(tg_state_dir)
    clean_db = root / "clean" / "search-v3" / "search.sqlite"
    if not clean_db.is_file():
        raise FileNotFoundError(f"Missing TG SQLite at {clean_db}")

    posts: dict[tuple[str, int], dict[str, Any]] = {}
    b01_failed_hashes: set[str] = set()

    with sqlite3.connect(f"file:{clean_db.resolve()}?mode=ro", uri=True, timeout=30.0) as conn:
        conn.row_factory = sqlite3.Row
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}

        if "tg_search_meta" in tables:
            for (k,) in conn.execute("SELECT key FROM tg_search_meta WHERE key LIKE 'b01fail:%'"):
                b01_failed_hashes.add(str(k))

        if "tg_raw_messages" in tables:
            for row in conn.execute("SELECT * FROM tg_raw_messages"):
                ch = row["channel_username"]
                mid = int(row["msg_id"])
                attachments = json.loads(row["attachment_names_json"] or "[]")
                link_items = json.loads(row["link_items_json"] or "[]")
                reply_ctx = json.loads(row["reply_context_json"] or "{}")
                fwd_ctx = json.loads(row["forward_context_json"] or "{}")
                posts[(ch, mid)] = {
                    "channel_username": ch,
                    "msg_id": mid,
                    "tg_post_url": row["tg_post_url"],
                    "published_timestamp": row["published_timestamp"],
                    "raw_text": row["raw_text"] or "",
                    "attachment_names": attachments,
                    "link_items": link_items,
                    "reply_context": reply_ctx,
                    "forward_context": fwd_ctx,
                    "shared_body_hash": row["shared_body_hash"],
                    "legacy_accepted": None,
                    "legacy_reason": None,
                    "device_codename": None,
                    "b01_failed": False,
                }

        if "tg_search_records" in tables:
            for row in conn.execute("SELECT * FROM tg_search_records"):
                payload = json.loads(row["payload"])
                ev = json.loads(row["evidence"])
                ch = str(payload.get("channel_username") or row["channel"]).lower().lstrip("@")
                mid = int(payload.get("msg_id") or row["msg_id"])
                key = (ch, mid)
                text = ev.get("observed_text") or ""
                if key not in posts:
                    att = [payload["file_attachment_name"]] if payload.get("file_attachment_name") else []
                    links = [{"url": u, "label": ""} for u in re.findall(r"https?://[^\s<>]+", text)]
                    posts[key] = {
                        "channel_username": ch,
                        "msg_id": mid,
                        "tg_post_url": payload.get("tg_post_url") or f"https://t.me/{ch}/{mid}",
                        "published_timestamp": payload.get("published_timestamp"),
                        "raw_text": text,
                        "attachment_names": att,
                        "link_items": links,
                        "reply_context": {},
                        "forward_context": {
                            "has_forward_header": payload.get("origin_type") == "native_forward",
                            "forwarded_from_url": payload.get("original_post_url")
                            if payload.get("origin_type") == "native_forward"
                            else None,
                        },
                        "shared_body_hash": compute_shared_body_hash(
                            {"text": text, "attachment_names": att, "link_items": links}
                        ),
                        "legacy_accepted": bool(row["accepted"]),
                        "legacy_reason": row["reason"],
                        "device_codename": row["device"],
                        "b01_failed": False,
                    }
                else:
                    if not posts[key]["raw_text"] and text:
                        posts[key]["raw_text"] = text
                    if posts[key]["legacy_accepted"] is None:
                        posts[key]["legacy_accepted"] = bool(row["accepted"])
                        posts[key]["legacy_reason"] = row["reason"]
                        posts[key]["device_codename"] = row["device"]

    # Recover missing texts from JSONL logs if needed
    missing = {k for k, v in posts.items() if not (v.get("raw_text") or "").strip()}
    if missing:
        for ldir in [root / "clean" / "search-v3" / "logs", root / "legacy" / "search-v3" / "logs"]:
            if not ldir.is_dir():
                continue
            for lpath in sorted(ldir.glob("*.jsonl")):
                try:
                    with lpath.open("r", encoding="utf-8", errors="replace") as fh:
                        for line in fh:
                            if '"resource_decision"' not in line:
                                continue
                            try:
                                ev = json.loads(line)
                            except ValueError:
                                continue
                            pkg = ev.get("package") or {}
                            ch = str(pkg.get("channel_username") or "").lower().lstrip("@")
                            mid = pkg.get("msg_id")
                            obs = ev.get("observed_text")
                            if (ch, mid) in missing and isinstance(obs, str) and obs.strip():
                                posts[(ch, mid)]["raw_text"] = obs
                                posts[(ch, mid)]["link_items"] = [
                                    {"url": u, "label": ""}
                                    for u in re.findall(r"https?://[^\s<>]+", obs)
                                ]
                                missing.discard((ch, mid))
                except OSError:
                    continue

    # Recover remaining from tg_http_cache
    if missing:
        with sqlite3.connect(f"file:{clean_db.resolve()}?mode=ro", uri=True, timeout=30.0) as conn:
            has_cache = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tg_http_cache'"
            ).fetchone()
            if has_cache:
                want_channels = {ch for ch, _ in missing}
                for (url, body) in conn.execute("SELECT url, body FROM tg_http_cache"):
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
                            if k in missing and (msg.get("text") or "").strip():
                                posts[k]["raw_text"] = msg["text"]
                                posts[k]["link_items"] = msg.get("link_items") or []
                                posts[k]["attachment_names"] = msg.get("attachment_names") or []
                                posts[k]["reply_context"] = {
                                    "reply_to_post_url": msg.get("reply_to_post_url"),
                                    "reply_to_msg_id": msg.get("reply_to_msg_id"),
                                    "reply_author": msg.get("reply_author"),
                                    "reply_text": msg.get("reply_text"),
                                }
                                missing.discard(k)
                    except (ValueError, UnicodeError, zlib.error, ChannelUnavailableError):
                        continue
                    if not missing:
                        break

    # Deduplicate by shared_body_hash and keep only posts with recoverable text or attachment
    seen_bodies: set[str] = set()
    pool: list[dict[str, Any]] = []
    for k in sorted(posts):
        rec = posts[k]
        if not (rec["raw_text"] or "").strip() and not rec["attachment_names"]:
            continue
        bhash = rec.get("shared_body_hash") or compute_shared_body_hash(
            {"text": rec["raw_text"], "attachment_names": rec["attachment_names"], "link_items": rec["link_items"]}
        )
        if bhash in seen_bodies:
            continue
        seen_bodies.add(bhash)
        rec["shared_body_hash"] = bhash
        rec["b01_failed"] = bool(b01_failed_hashes) and rec["legacy_reason"] == "source_text_not_recoverable"
        rec["stratum"] = classify_stratum(rec)
        rec["classifier_input"] = format_classifier_input(
            rec["raw_text"],
            rec["attachment_names"],
            rec["link_items"],
            rec["reply_context"],
        )
        pool.append(rec)
    return pool


def _stratified_take(
    items: list[dict[str, Any]],
    target_count: int,
    rng: random.Random,
    *,
    hard_weights: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    if len(items) <= target_count:
        return list(items)
    by_stratum: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        by_stratum[item["stratum"]].append(item)
    for bucket in by_stratum.values():
        rng.shuffle(bucket)

    weights = hard_weights or {
        "hard_request_or_question": 0.22,
        "hard_teaser_or_wip": 0.12,
        "short_with_attachment_or_link": 0.18,
        "short_text_only": 0.10,
        "non_english_multilingual": 0.12,
        "rejected_or_b01fail": 0.12,
        "long_changelog_or_release": 0.07,
        "standard_post": 0.07,
    }

    selected: list[dict[str, Any]] = []
    used_ids: set[tuple[str, int]] = set()
    for stratum, ratio in weights.items():
        quota = max(1, int(round(target_count * ratio)))
        for item in by_stratum.get(stratum, [])[:quota]:
            key = (item["channel_username"], item["msg_id"])
            if key not in used_ids:
                selected.append(item)
                used_ids.add(key)
                if len(selected) >= target_count:
                    return selected

    remaining = [
        item for item in items if (item["channel_username"], item["msg_id"]) not in used_ids
    ]
    rng.shuffle(remaining)
    for item in remaining:
        selected.append(item)
        if len(selected) >= target_count:
            break
    return selected[:target_count]


def export_annotation_datasets(
    tg_state_dir: str | Path,
    output_dir: str | Path,
    *,
    train_size: int = 1800,
    val_size: int = 200,
    seed: int = 20261122,
) -> dict[str, Any]:
    """Export channel-disjoint train_candidates_2000.jsonl and golden_val_200.jsonl."""
    pool = load_candidate_pool(tg_state_dir)
    if not pool:
        raise RuntimeError("No recoverable TG posts found in state directory")

    rng = random.Random(seed)
    by_channel: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in pool:
        by_channel[item["channel_username"]].append(item)

    channels = sorted(by_channel.keys())
    rng.shuffle(channels)

    # Reserve ~15-20% of channels exclusively for golden_val_200.jsonl (disjoint channel split).
    val_pool: list[dict[str, Any]] = []
    train_pool: list[dict[str, Any]] = []
    val_channels: set[str] = set()
    val_target_pool = max(val_size * 3, int(len(pool) * 0.15))
    for ch in channels:
        if len(val_pool) < val_target_pool and len(channels) > 1:
            val_channels.add(ch)
            val_pool.extend(by_channel[ch])
        else:
            train_pool.extend(by_channel[ch])

    if not train_pool:
        # Fallback when pool has only 1 channel: split strictly by published_timestamp/msg_id
        ordered = sorted(pool, key=lambda x: (x.get("published_timestamp") or 0, x["msg_id"]))
        cut = max(1, len(ordered) - val_size)
        train_pool = ordered[:cut]
        val_pool = ordered[cut:]

    val_selected = _stratified_take(val_pool, min(val_size, len(val_pool)), rng)
    train_selected = _stratified_take(train_pool, min(train_size, len(train_pool)), rng)

    out_root = Path(output_dir)
    out_root.mkdir(parents=True, exist_ok=True)
    train_path = out_root / "train_candidates_2000.jsonl"
    val_path = out_root / "golden_val_200.jsonl"

    def _to_export_row(item: dict[str, Any], split: str) -> dict[str, Any]:
        sid = hashlib.sha256(
            f"{item['channel_username']}|{item['msg_id']}".encode("utf-8")
        ).hexdigest()[:16]
        return {
            "sample_id": sid,
            "split": split,
            "stratum": item["stratum"],
            "channel_username": item["channel_username"],
            "msg_id": item["msg_id"],
            "tg_post_url": item["tg_post_url"],
            "published_timestamp": item["published_timestamp"],
            "raw_text": item["raw_text"],
            "attachment_names": item["attachment_names"],
            "link_items": item["link_items"],
            "reply_context": item["reply_context"],
            "forward_context": item["forward_context"],
            "classifier_input": item["classifier_input"],
            "legacy_hints": {
                "note": "Reference only; Question 1 intent label must be independent of site policy",
                "legacy_accepted": item["legacy_accepted"],
                "legacy_reason": item["legacy_reason"],
                "device_codename": item["device_codename"],
            },
            "label": None,
            "allowed_labels": ["release_related", "irrelevant", "insufficient_context"],
            "notes": "",
        }

    with train_path.open("w", encoding="utf-8") as fh:
        for row in train_selected:
            fh.write(json.dumps(_to_export_row(row, "train"), ensure_ascii=False) + "\n")

    with val_path.open("w", encoding="utf-8") as fh:
        for row in val_selected:
            fh.write(json.dumps(_to_export_row(row, "golden_val"), ensure_ascii=False) + "\n")

    summary = {
        "total_unique_pool": len(pool),
        "train_exported": len(train_selected),
        "golden_val_exported": len(val_selected),
        "train_path": str(train_path),
        "golden_val_path": str(val_path),
        "train_channels": len({r["channel_username"] for r in train_selected}),
        "val_channels": len({r["channel_username"] for r in val_selected}),
        "channel_overlap": len(
            {r["channel_username"] for r in train_selected}
            & {r["channel_username"] for r in val_selected}
        ),
        "train_strata": dict(Counter(r["stratum"] for r in train_selected)),
        "val_strata": dict(Counter(r["stratum"] for r in val_selected)),
    }
    (out_root / "sampling_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--state-dir",
        default="/var/lib/findmyrom/catalogue-v3/tg-v4",
        help="103 TG state directory",
    )
    parser.add_argument(
        "--output-dir",
        default="/var/lib/findmyrom/catalogue-v3/tg-v4/datasets",
        help="Output directory for JSONL annotation datasets",
    )
    parser.add_argument("--train-size", type=int, default=1800)
    parser.add_argument("--val-size", type=int, default=200)
    parser.add_argument("--seed", type=int, default=20261122)
    args = parser.parse_args(argv)
    summary = export_annotation_datasets(
        args.state_dir,
        args.output_dir,
        train_size=args.train_size,
        val_size=args.val_size,
        seed=args.seed,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
