"""Tests for TG Pipeline v2 Phase 4: attributed_extractor, 3-layer DB persistence,
pending evidence queue, retry_wait transient isolation, and v2_cleaner incremental loop.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from fmr_tg_scanner.attributed_extractor import (
    DeepSeekAttributedClient,
    compute_extraction_cache_key,
    normalize_and_verify_extraction,
)
from fmr_tg_scanner.evidence_store import EvidenceStore
from fmr_tg_scanner.v2_cleaner import clean_v2_once


class _DummyClassifier:
    """Deterministic pre-E5/E5 router stub so offline unit tests stay fast without torch/onnx."""

    def __init__(self, forced_routes: dict[str, dict] | None = None) -> None:
        self.forced_routes = forced_routes or {}
        from fmr_tg_scanner.official_registry import load_official_registry

        self.registry = load_official_registry()

    def route_message(self, message: dict) -> dict:
        pk = message["post_key"]
        if pk in self.forced_routes:
            return self.forced_routes[pk]
        blocked, info = self.registry.is_layer1_blocked_channel(message.get("channel_username"))
        if blocked:
            cat = (info or {}).get("category", "blocked")
            return {
                "layer": "layer1_channel_blocklist",
                "action": "skip_channel_blocked",
                "send_to_deepseek": False,
                "reason": f"layer1_blocked_channel:{cat}",
                "blindspot_retained": False,
                "audit_sampled": False,
                "probs": None,
                "pred_label": "irrelevant",
            }
        return {
            "layer": "layer2_e5_onnx",
            "action": "send_to_deepseek",
            "send_to_deepseek": True,
            "reason": "e5_release_or_ambiguous",
            "blindspot_retained": False,
            "audit_sampled": False,
            "probs": {"release_related": 0.92, "irrelevant": 0.06, "insufficient_context": 0.02},
            "pred_label": "release_related",
        }


SAMPLE_CATALOGUE = {
    "alioth": ["POCO F3", "Mi 11X", "Redmi K40"],
    "marble": ["POCO F5", "Redmi Note 12 Turbo"],
    "sweet": ["Redmi Note 10 Pro"],
    "tapas": ["Redmi Note 12 4G"],
    "monet": ["Mi 10 Lite 5G"],
}


def _make_state_dir(tmp_path: Path) -> tuple[Path, EvidenceStore]:
    root = tmp_path / "tg-v4"
    db_path = root / "clean" / "search-v3" / "search.sqlite"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    (root / "initialized.json").write_text('{"workers":4}', encoding="utf-8")
    store = EvidenceStore(db_path)
    return root, store


def _base_msg(channel: str, msg_id: int, text: str, **extra) -> dict:
    return {
        "channel_username": channel,
        "msg_id": msg_id,
        "tg_post_url": f"https://t.me/{channel}/{msg_id}",
        "published_timestamp": 1763800000 + msg_id,
        "text": text,
        "attachment_names": extra.pop("attachment_names", []),
        "link_items": extra.pop("link_items", []),
        **extra,
    }


def _valid_model_payload(**overrides) -> dict:
    base = {
        "subject_summary": "Unofficial build released for target device.",
        "is_release": True,
        "is_withdrawal": False,
        "resource_kind": "rom",
        "project_name": {"value": "crDroid", "evidence_quote": "crDroid 10.6 Unofficial"},
        "release_status": {"value": "unofficial", "evidence_quote": "Unofficial"},
        "release_stage": "stable",
        "is_paid_access": {"value": False, "evidence_quote": None},
        "is_oem_stock": False,
        "project_version": {"value": "10.6", "evidence_quote": "crDroid 10.6"},
        "android_version": {"value": "14", "evidence_quote": "Android 14"},
        "kernel_version": {"value": None, "evidence_quote": None},
        "required_firmware": {"value": None, "evidence_quote": None},
        "system_base": "AOSP",
        "build_date": None,
        "build_variants": ["gapps"],
        "package_form": "device_specific",
        "target_devices": {"codenames": ["alioth"], "marketing_names": ["POCO F3"], "evidence_quote": "POCO F3 (alioth)"},
        "donor_devices": [],
        "excluded_devices": [],
        "maintainer": {"handle": "@alidev", "display_name": "Ali Dev", "evidence_quote": "Maintainer: Ali Dev (@alidev)"},
        "credits_or_thanks": [],
    }
    base.update(overrides)
    return base


def test_attribution_donor_exclusion_and_official_trees_defense(tmp_path: Path) -> None:
    msg = _base_msg(
        "mixromhub",
        101,
        "HyperOS 2.0 Port | Android 15\nFor POCO F5 (marble) ported from POCO F3 (alioth).\n"
        "Not for sweet.\nBased on Xiaomi Official Trees.\nMaintainer: Porter (@porterdev)\nCredits: @helper1",
        link_items=[{"url": "https://sourceforge.net/projects/mix/files/hyperos.zip", "label": "Download"}],
    )
    raw = _valid_model_payload(
        resource_kind="rom",
        project_name={"value": "HyperOS", "evidence_quote": "HyperOS 2.0 Port"},
        release_status={"value": "official", "evidence_quote": "Based on Xiaomi Official Trees"},
        is_paid_access={"value": True, "evidence_quote": "BuyMeACoffee"},  # false positive from model
        project_version={"value": "2.0", "evidence_quote": "HyperOS 2.0"},
        android_version={"value": "15", "evidence_quote": "Android 15"},
        system_base="HyperOS",
        # Deliberately include donor 'alioth' and excluded 'sweet' in target_codenames to test isolation
        target_devices={
            "codenames": ["marble", "alioth", "sweet", "ghost_codename"],
            "marketing_names": ["POCO F5"],
            "evidence_quote": "For POCO F5 (marble)",
        },
        donor_devices=["alioth", "POCO F3"],
        excluded_devices=["sweet"],
        maintainer={"handle": "@porterdev", "display_name": "Porter", "evidence_quote": "Maintainer: Porter (@porterdev)"},
        credits_or_thanks=["@helper1"],
    )

    root, store = _make_state_dir(tmp_path)
    store.ingest_message(msg, now=1000.0)
    store.close()

    async def fake_requester(payload: dict):
        return json.dumps(raw, ensure_ascii=False), {"completion_tokens": 90}

    client = DeepSeekAttributedClient(api_key="test-key", requester=fake_requester)
    res = clean_v2_once(
        root,
        catalogue_names=SAMPLE_CATALOGUE,
        sync_workers=False,
        classifier=_DummyClassifier(),
        llm_client=client,
        now=1005.0,
    )

    assert res["status"] == "outbox_enqueued"
    assert res["counts"]["outbox_added"] == 1

    store = EvidenceStore(root / "clean" / "search-v3" / "search.sqlite")
    try:
        outbox = store.query("SELECT * FROM tg_publish_outbox")
        assert len(outbox) == 1
        assert outbox[0]["device_codename"] == "marble"  # alioth (donor), sweet (excluded), ghost_codename all blocked!
        payload = json.loads(outbox[0]["payload_json"])
        assert payload["package"]["project_name"] == "HyperOS"
        # Downgraded from false 'official' ("Official Trees")
        assert payload["package"]["taxonomy"]["project_recognition"] != "official"
        assert "alioth" in payload["package"]["taxonomy"]["evidence"]["attributed_extraction"]["donor_devices"]

        ledger_rows = store.query("SELECT * FROM tg_extraction_ledger")
        assert len(ledger_rows) == 1
        assert json.loads(ledger_rows[0]["raw_model_json"])["is_release"] is True
        assert json.loads(ledger_rows[0]["normalized_json"])["release_status"]["value"] == "unknown"
        assert json.loads(ledger_rows[0]["published_json"])["publishable_keys"] == [f"marble|{msg['tg_post_url']}"]
    finally:
        store.close()


def test_missing_project_version_routes_to_pending_evidence_not_android(tmp_path: Path) -> None:
    msg = _base_msg(
        "mixromhub",
        102,
        "LineageOS Unofficial | Android 14\nDevice: POCO F3 (alioth)\nBy @alidev",
        link_items=[{"url": "https://github.com/dev/rel/v1.zip", "label": "ROM"}],
    )
    # Hallucinate project_version == android_version without explicit project version marker
    raw = _valid_model_payload(
        project_name={"value": "LineageOS", "evidence_quote": "LineageOS Unofficial"},
        project_version={"value": "14", "evidence_quote": "Android 14"},
        android_version={"value": "14", "evidence_quote": "Android 14"},
        maintainer={"handle": "@alidev", "display_name": None, "evidence_quote": "By @alidev"},
    )
    root, store = _make_state_dir(tmp_path)
    store.ingest_message(msg, now=1000.0)
    store.close()

    async def fake_requester(_payload: dict):
        return json.dumps(raw), {}

    res = clean_v2_once(
        root,
        catalogue_names=SAMPLE_CATALOGUE,
        sync_workers=False,
        classifier=_DummyClassifier(),
        llm_client=DeepSeekAttributedClient(api_key="k", requester=fake_requester),
        now=1005.0,
    )
    assert res["counts"]["pending_evidence_enqueued"] == 1
    assert res["counts"].get("outbox_added", 0) == 0

    store = EvidenceStore(root / "clean" / "search-v3" / "search.sqlite")
    try:
        pe = store.query("SELECT * FROM tg_pending_evidence_queue")
        assert len(pe) == 1
        assert pe[0]["device_codename"] == "alioth"
        assert "missing_release_version" in json.loads(pe[0]["missing_fields_json"])
        assert store.query("SELECT COUNT(*) n FROM tg_publish_outbox")[0]["n"] == 0
    finally:
        store.close()


def test_layer3_policy_gates_official_paid_stock_donate_matrixx(tmp_path: Path) -> None:
    msg_template = lambda mid, text, links=None: _base_msg("mixromhub", mid, text, link_items=links or [])

    # 1) Active official project crDroid -> EXCLUDE
    norm_crdroid_off = normalize_and_verify_extraction(
        _valid_model_payload(
            project_name={"value": "crDroid", "evidence_quote": "crDroid 10.6 Official"},
            release_status={"value": "official", "evidence_quote": "crDroid 10.6 Official Build"},
        ),
        msg_template(1, "crDroid 10.6 Official Build | Android 14\nFor POCO F3 (alioth)\nMaintainer: Ali Dev (@alidev)"),
    )
    assert norm_crdroid_off["decision"] == "excluded_official_project"

    # 2) Archived official project Project Matrixx (exclude_official_in_tg: false) -> KEEP!
    norm_matrixx = normalize_and_verify_extraction(
        _valid_model_payload(
            project_name={"value": "Project Matrixx", "evidence_quote": "Project Matrixx v10.8 Official"},
            release_status={"value": "official", "evidence_quote": "Project Matrixx v10.8 Official"},
            project_version={"value": "v10.8", "evidence_quote": "Project Matrixx v10.8"},
        ),
        msg_template(2, "Project Matrixx v10.8 Official | Android 14\nFor POCO F3 (alioth)\nMaintainer: Ali Dev (@alidev)"),
    )
    assert norm_matrixx["decision"] == "eligible"

    # 3) Paywalled ROM -> EXCLUDE
    norm_paid = normalize_and_verify_extraction(
        _valid_model_payload(
            is_paid_access={"value": True, "evidence_quote": "VIP subscription required to download"},
        ),
        msg_template(3, "crDroid 10.6 Unofficial | Android 14\nVIP subscription required to download\nPOCO F3 (alioth)\n@alidev"),
    )
    assert norm_paid["decision"] == "excluded_paid_resource"

    # 4) Free ROM with voluntary Donate via PayPal/BuyMeACoffee -> KEEP!
    norm_donate = normalize_and_verify_extraction(
        _valid_model_payload(
            is_paid_access={"value": True, "evidence_quote": "donate via PayPal or BuyMeACoffee"},
        ),
        msg_template(4, "crDroid 10.6 Unofficial | Android 14\nPOCO F3 (alioth)\nMaintainer: Ali Dev (@alidev)\nIf you like my work, donate via PayPal or BuyMeACoffee"),
    )
    assert norm_donate["decision"] == "eligible"

    # 5) Mixed-channel fw_*.zip OEM stock firmware -> EXCLUDE
    norm_fw = normalize_and_verify_extraction(
        _valid_model_payload(resource_kind="firmware", is_oem_stock=True),
        msg_template(5, "Firmware fw_alioth_OS1.0.3.0.zip | Android 14\nPOCO F3 (alioth)"),
    )
    assert norm_fw["decision"] == "excluded_stock_firmware"


def test_cross_channel_cache_reuse_edit_update_and_explicit_withdraw(tmp_path: Path) -> None:
    calls = 0
    body = "crDroid 10.6 Unofficial | Android 14\nFor POCO F3 (alioth)\nMaintainer: Ali Dev (@alidev)"
    msg_a = _base_msg("chan_a", 10, body, link_items=[{"url": "https://sourceforge.net/files/cr10.6.zip", "label": "DL"}])
    msg_b = _base_msg(
        "chan_b",
        20,
        body,
        link_items=[{"url": "https://sourceforge.net/files/cr10.6.zip", "label": "DL"}],
        has_forward_header=True,
        forwarded_from_url="https://t.me/chan_a/10",
    )

    assert compute_extraction_cache_key(msg_a, model_name="deepseek-flash") == compute_extraction_cache_key(
        msg_b, model_name="deepseek-flash"
    )

    raw_v1 = _valid_model_payload()
    raw_v2 = _valid_model_payload(
        project_name={"value": "crDroid", "evidence_quote": "crDroid 10.7 Unofficial"},
        project_version={"value": "10.7", "evidence_quote": "crDroid 10.7"},
    )
    mode = {"ver": "v1"}

    async def fake_requester(_payload: dict):
        nonlocal calls
        calls += 1
        if mode["ver"] == "v1":
            return json.dumps(raw_v1), {}
        if mode["ver"] == "v2":
            return json.dumps(raw_v2), {}
        return json.dumps({"is_release": True, "is_withdrawal": True, "resource_kind": "rom", "subject_summary": "Build retracted due to bootloop"}), {}

    root, store = _make_state_dir(tmp_path)
    store.ingest_message(msg_a, now=100.0)
    store.ingest_message(msg_b, now=101.0)
    store.close()

    client = DeepSeekAttributedClient(api_key="k", requester=fake_requester)
    res1 = clean_v2_once(
        root, catalogue_names=SAMPLE_CATALOGUE, sync_workers=False, classifier=_DummyClassifier(), llm_client=client, now=105.0
    )
    assert calls == 1  # chan_b reused chan_a extraction via cache!
    assert res1["counts"]["extraction_cache_hits"] == 1
    assert res1["counts"]["outbox_added"] == 2

    # Simulate publisher committing both to tg_publish_ledger as active
    store = EvidenceStore(root / "clean" / "search-v3" / "search.sqlite")
    out_rows = store.query("SELECT * FROM tg_publish_outbox WHERE status='pending'")
    for r in out_rows:
        p = json.loads(r["payload_json"])["package"]
        store.execute("UPDATE tg_publish_outbox SET status='done' WHERE id=?", (r["id"],))
        store.execute(
            "INSERT INTO tg_publish_ledger (resource_key, public_token, device_codename, target_post_url, revision_id, payload_hash, status, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 'h1', 'active', 110)",
            (r["resource_key"], r["public_token"], r["device_codename"], p["tg_post_url"], r["revision_id"]),
        )

    # Rule 3: chan_a edits msg 10 to version 10.7 -> new cache key -> UPDATE enqueued!
    mode["ver"] = "v2"
    msg_a_v2 = _base_msg(
        "chan_a",
        10,
        "crDroid 10.7 Unofficial | Android 14\nFor POCO F3 (alioth)\nMaintainer: Ali Dev (@alidev)",
        link_items=[{"url": "https://sourceforge.net/files/cr10.7.zip", "label": "DL"}],
    )
    store.ingest_message(msg_a_v2, now=200.0)
    store.close()

    res2 = clean_v2_once(
        root, catalogue_names=SAMPLE_CATALOGUE, sync_workers=False, classifier=_DummyClassifier(), llm_client=client, now=205.0
    )
    assert calls == 2
    assert res2["counts"]["outbox_updated"] == 1

    # Explicit withdrawal post replying to chan_b/20 -> enqueues WITHDRAW only for chan_b/20
    mode["ver"] = "withdraw"
    store = EvidenceStore(root / "clean" / "search-v3" / "search.sqlite")
    withdraw_msg = _base_msg(
        "chan_b",
        25,
        "Previous crDroid build removed/retracted due to bootloop!",
        reply_to_post_url="https://t.me/chan_b/20",
        reply_to_msg_id=20,
    )
    store.ingest_message(withdraw_msg, now=300.0)
    store.close()

    res3 = clean_v2_once(
        root, catalogue_names=SAMPLE_CATALOGUE, sync_workers=False, classifier=_DummyClassifier(), llm_client=client, now=305.0
    )
    assert res3["counts"]["outbox_withdrawn"] == 1
    store = EvidenceStore(root / "clean" / "search-v3" / "search.sqlite")
    wd = store.query("SELECT resource_key, op_type FROM tg_publish_outbox WHERE op_type='WITHDRAW'")
    assert len(wd) == 1
    assert wd[0]["resource_key"] == "alioth|https://t.me/chan_b/20"
    store.close()


def test_transient_llm_error_only_retries_never_withdraws_and_dry_run_is_clean(tmp_path: Path) -> None:
    msg_ok = _base_msg(
        "mixromhub",
        301,
        "crDroid 10.6 Unofficial | Android 14\nFor POCO F3 (alioth)\nMaintainer: Ali Dev (@alidev)",
        link_items=[{"url": "https://github.com/a/b.zip", "label": "ROM"}],
    )
    msg_fail = _base_msg(
        "mixromhub",
        302,
        "PixelOS 14 Unofficial | Android 14\nFor POCO F5 (marble)\nMaintainer: Ali Dev (@alidev)",
        link_items=[{"url": "https://github.com/a/c.zip", "label": "ROM"}],
    )
    root, store = _make_state_dir(tmp_path)
    store.ingest_message(msg_ok, now=100.0)
    store.ingest_message(msg_fail, now=101.0)
    store.close()

    async def partial_requester(payload: dict):
        content = payload["messages"][1]["content"]
        if "marble" in content:
            from fmr_tg_scanner.attributed_extractor import TransientLLMError

            raise TransientLLMError("llm_http_429")
        return json.dumps(_valid_model_payload()), {}

    client = DeepSeekAttributedClient(api_key="k", requester=partial_requester)
    # Dry run first: zero mutations!
    dry = clean_v2_once(
        root, catalogue_names=SAMPLE_CATALOGUE, sync_workers=False, dry_run=True, classifier=_DummyClassifier(), llm_client=client, now=105.0
    )
    assert dry["dry_run"] is True
    store = EvidenceStore(root / "clean" / "search-v3" / "search.sqlite")
    assert [r["status"] for r in store.query("SELECT status FROM tg_cleaning_queue")] == ["pending", "pending"]
    assert store.query("SELECT COUNT(*) n FROM tg_publish_outbox")[0]["n"] == 0
    store.close()

    # Live cycle with 1 transient 429 + 1 success
    res = clean_v2_once(
        root, catalogue_names=SAMPLE_CATALOGUE, sync_workers=False, classifier=_DummyClassifier(), llm_client=client, now=110.0
    )
    assert res["counts"]["outbox_added"] == 1
    assert res["counts"]["llm_calls_failed_retry_wait"] == 1

    store = EvidenceStore(root / "clean" / "search-v3" / "search.sqlite")
    try:
        rows = {r["post_key"]: r for r in store.query("SELECT post_key, process_status FROM tg_raw_messages")}
        assert rows["mixromhub|301"]["process_status"] == "published_outbox"
        assert rows["mixromhub|302"]["process_status"] == "retry_wait"
        q_row = store.query("SELECT status, attempts, next_retry_at FROM tg_cleaning_queue WHERE post_key='mixromhub|302'")[0]
        assert q_row["status"] == "retry_wait"
        assert q_row["attempts"] == 1
        assert q_row["next_retry_at"] == 110.0 + 900
        assert store.query("SELECT COUNT(*) n FROM tg_publish_outbox WHERE op_type='WITHDRAW'")[0]["n"] == 0
    finally:
        store.close()


def test_worker_snapshot_sync_preserves_clean_status(tmp_path: Path) -> None:
    root = tmp_path / "tg-v4"
    (root / "clean" / "search-v3").mkdir(parents=True, exist_ok=True)
    (root / "workers" / "0" / "search-v3").mkdir(parents=True, exist_ok=True)
    clean_store = EvidenceStore(root / "clean" / "search-v3" / "search.sqlite")
    worker_store = EvidenceStore(root / "workers" / "0" / "search-v3" / "search.sqlite")
    msg = _base_msg("mixromhub", 500, "crDroid 10.6 Unofficial | POCO F3 (alioth)")

    clean_store.ingest_message(msg, now=100.0)
    clean_store.mark_cleaning_result("mixromhub|500", process_status="published_outbox", process_reason="done", now=101.0)
    worker_store.ingest_message(msg, now=200.0)  # identical content_revision_hash, later observed timestamp
    worker_store.close()

    clean_store.sync_raw_and_queue_from_snapshot(root / "workers" / "0" / "search-v3" / "search.sqlite")
    row = clean_store.query("SELECT process_status, last_observed_at FROM tg_raw_messages WHERE post_key='mixromhub|500'")[0]
    assert row["process_status"] == "published_outbox"
    assert row["last_observed_at"] == 200.0
    q = clean_store.query("SELECT status FROM tg_cleaning_queue WHERE post_key='mixromhub|500'")[0]
    assert q["status"] == "done"
    clean_store.close()


def test_v2_cleaner_wires_real_intent_classifier_signature(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import inspect
    import fmr_tg_scanner.v2_cleaner as vc
    from fmr_tg_scanner.intent_classifier import IntentClassifier

    sig = inspect.signature(IntentClassifier.__init__)
    assert "registry" in sig.parameters

    captured: dict = {}

    class _FakeInitClassifier:
        def __init__(self, model_dir, *, registry=None) -> None:
            captured["model_dir"] = Path(model_dir)
            captured["registry"] = registry

        def route_message(self, message: dict) -> dict:
            return {"action": "skip_irrelevant", "reason": "fake", "audit_sampled": False, "probs": {}}

    monkeypatch.setattr(vc, "IntentClassifier", _FakeInitClassifier)
    root, store = _make_state_dir(tmp_path)
    store.ingest_message(_base_msg("mixromhub", 600, "random chat message that stays test"), now=10.0)
    store.close()

    res = clean_v2_once(
        root,
        model_dir=tmp_path / "m",
        catalogue_names=SAMPLE_CATALOGUE,
        sync_workers=False,
        now=12.0,
    )
    assert res["counts"]["e5_skipped_irrelevant"] == 1
    assert captured["model_dir"] == tmp_path / "m"
    assert captured["registry"] is not None
