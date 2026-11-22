"""Tests for v2 SQLite Evidence Store, 3-rule dedup, 5% blindspot sampling, and 67k migration."""
import hashlib
import json

from fmr_tg_scanner.evidence_store import (
    EvidenceStore,
    compute_content_revision_hash,
    compute_shared_body_hash,
    migrate_existing_state_to_v2,
)
from fmr_tg_scanner.html_parser import parse_tg_channel_page
from fmr_tg_scanner.search_store import SearchStore


def test_three_dedup_rules_and_same_transaction_incremental_queue(tmp_path):
    store = EvidenceStore(tmp_path / "evidence.sqlite")
    try:
        base_msg = {
            "channel_username": "poco_f3_updates",
            "msg_id": 101,
            "tg_post_url": "https://t.me/poco_f3_updates/101",
            "text": "LineageOS 22.1 Unofficial for POCO F3 (alioth) | Android 15\nDownload: https://example.com/los-1.zip",
            "attachment_names": [],
            "link_items": [{"url": "https://example.com/los-1.zip", "label": "Download"}],
        }
        r1 = store.ingest_message(base_msg, now=100.0)
        assert r1["action"] == "inserted" and r1["enqueued"] is True

        # Rule 1: Same post & same content_revision_hash -> 0 new queue writes
        r2 = store.ingest_message(base_msg, now=200.0)
        assert r2["action"] == "unchanged_revision_reused" and r2["enqueued"] is False

        # Rule 2: Different channel forwarding identical content -> same shared_body_hash, separate provenance
        fwd_msg = {
            **base_msg,
            "channel_username": "android_roms_mirror",
            "msg_id": 505,
            "tg_post_url": "https://t.me/android_roms_mirror/505",
            "has_forward_header": True,
            "forwarded_from_url": "https://t.me/poco_f3_updates/101",
        }
        r3 = store.ingest_message(fwd_msg, now=300.0)
        assert r3["action"] == "inserted"
        assert r3["shared_body_hash"] == r1["shared_body_hash"]
        assert r3["content_revision_hash"] == compute_content_revision_hash(fwd_msg)

        # Rule 3: Version/link edited -> content_revision_hash changes -> re-enqueued
        edited_msg = {
            **base_msg,
            "text": "LineageOS 22.2 Unofficial for POCO F3 (alioth) | Android 15\nDownload: https://example.com/los-2.zip",
            "link_items": [{"url": "https://example.com/los-2.zip", "label": "Download"}],
        }
        r4 = store.ingest_message(edited_msg, now=400.0)
        assert r4["action"] == "updated_revision" and r4["content_revision_no"] == 2 and r4["enqueued"] is True

        due = store.fetch_due_cleaning_batch(limit=10, now=500.0)
        assert {d["post_key"] for d in due} == {"poco_f3_updates|101", "android_roms_mirror|505"}

        # Transient API error only sets retry_wait, never irrelevant
        store.mark_cleaning_retry("poco_f3_updates|101", "HTTP 429 rate limit", cooldown_seconds=900, now=500.0)
        row = store.query("SELECT process_status FROM tg_raw_messages WHERE post_key='poco_f3_updates|101'")[0]
        assert row["process_status"] == "retry_wait"
        due_after = store.fetch_due_cleaning_batch(limit=10, now=600.0)
        assert [d["post_key"] for d in due_after] == ["android_roms_mirror|505"]
    finally:
        store.close()


def test_five_percent_blindspot_sampling_retains_fraction_of_hard_filtered_posts(tmp_path):
    store = EvidenceStore(tmp_path / "blindspot.sqlite")
    try:
        retained = 0
        filtered = 0
        for i in range(1, 201):
            res = store.ingest_message(
                {
                    "channel_username": "chatgroup",
                    "msg_id": i,
                    "text": "ok",
                },
                now=100.0,
            )
            if res["is_blindspot_sample"]:
                retained += 1
                assert res["process_status"] == "blindspot_retained"
            else:
                filtered += 1
                assert res["process_status"] == "hard_filtered"
        assert 2 <= retained <= 25
        assert retained + filtered == 200
    finally:
        store.close()


def test_html_parser_extracts_reply_context_media_group_and_attachments():
    html = """
    <section class="tgme_widget_history" data-peer-id="-100123456">
      <div class="tgme_widget_message" data-post="aliothdev/42" data-grouped-id="999888">
        <a class="tgme_widget_message_reply" href="https://t.me/aliothdev/40">
          <div class="tgme_widget_message_author_name">Dev</div>
          <div class="tgme_widget_message_metatext">Evolution X 10.1</div>
        </a>
        <div class="tgme_widget_message_document_title">EvoX-patch.zip</div>
        <div class="tgme_widget_message_text">Fixed mirror link <a href="https://mirror.example.com/evox.zip">Mirror</a></div>
        <time datetime="2026-11-20T10:00:00Z"></time>
      </div>
    </section>
    """
    parsed = parse_tg_channel_page(html, "aliothdev")
    msg = parsed["messages"][0]
    assert msg["channel_id"] == "-100123456"
    assert msg["reply_to_msg_id"] == 40
    assert msg["reply_to_post_url"] == "https://t.me/aliothdev/40"
    assert msg["reply_author"] == "Dev"
    assert msg["media_group_id"] == "999888"
    assert msg["attachment_names"] == ["EvoX-patch.zip"]


def test_migrate_existing_state_marks_uncleaned_backlog_and_baselines_ledger(tmp_path):
    root = tmp_path / "tg-v4"
    clean_dir = root / "clean" / "search-v3"
    clean_dir.mkdir(parents=True)
    legacy = SearchStore(clean_dir / "search.sqlite")
    for idx, accepted in [(1, True), (2, False)]:
        text = f"LineageOS 22.{idx} for alioth"
        pkg = {
            "device_codename": "alioth",
            "channel_username": "testchan",
            "msg_id": idx,
            "tg_post_url": f"https://t.me/testchan/{idx}",
            "raw_text_hash": hashlib.sha256(text.encode()).hexdigest(),
            "delivery_type": "telegram_message",
        }
        legacy.save(pkg, {"observed_text": text}, "custom_or_port" if accepted else "unknown", "ok" if accepted else "rej", accepted)
    # Clear v2 tables to simulate pre-v2 legacy state
    legacy.execute("DELETE FROM tg_raw_messages")
    legacy.execute("DELETE FROM tg_cleaning_queue")
    legacy.close()

    summary = migrate_existing_state_to_v2(root, now=2000.0)
    assert summary["unique_posts_migrated"] == 2
    assert summary["uncleaned_backlog_marked"] == 2
    assert summary["baselined_publish_ledger_entries"] == 1

    store = EvidenceStore(clean_dir / "search.sqlite")
    try:
        statuses = {r["post_key"]: r["process_status"] for r in store.query("SELECT post_key, process_status FROM tg_raw_messages")}
        assert statuses == {"testchan|1": "uncleaned_backlog", "testchan|2": "uncleaned_backlog"}
        assert store.query("SELECT COUNT(*) n FROM tg_cleaning_queue")[0]["n"] == 0
        ledger = store.query("SELECT * FROM tg_publish_ledger")
        assert len(ledger) == 1 and ledger[0]["status"] == "active"
    finally:
        store.close()
