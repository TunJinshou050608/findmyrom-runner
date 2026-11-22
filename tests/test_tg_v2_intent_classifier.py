"""Tests for Phase 3 IntentClassifier & IntentTrainer (pure metadata + ONNX gate)."""
from fmr_tg_scanner.intent_classifier import (
    IntentClassifier,
    extract_structural_features,
)
from fmr_tg_scanner.intent_trainer import (
    calibrate_safe_thresholds,
    stratified_group_kfold,
)


def test_structural_features_have_zero_official_regex_and_capture_metadata():
    # Official/Unofficial strings must NOT alter structural features
    msg_a = {
        "raw_text": "Evolution X 10.2 Official | Android 15\nDownload: https://dl.example.com/evox.zip",
        "attachment_names": [],
        "link_items": [{"url": "https://dl.example.com/evox.zip", "label": "Download"}],
    }
    msg_b = {
        "raw_text": "Evolution X 10.2 Unofficial | Android 15\nDownload: https://dl.example.com/evox.zip",
        "attachment_names": [],
        "link_items": [{"url": "https://dl.example.com/evox.zip", "label": "Download"}],
    }
    fa = extract_structural_features(msg_a)
    fb = extract_structural_features(msg_b)
    # Only length differs slightly ("Official" vs "Unofficial"); structural flags must match
    assert list(fa[:8]) == list(fb[:8])
    assert list(fa[10:]) == list(fb[10:])
    assert fa[1] == 1.0  # has_external_link
    assert fa[7] == 1.0  # has_version_digit
    assert fa[11] == 0.0  # no_download_and_no_version

    # Template update without version/link/attachment
    bare = {
        "raw_text": "New OS Fastboot Update Available for POCO F6!\nRegion: EEA",
        "attachment_names": [],
        "link_items": [],
    }
    fc = extract_structural_features(bare)
    assert fc[0] == 0.0 and fc[1] == 0.0 and fc[7] == 0.0 and fc[11] == 1.0


def test_stratified_group_kfold_keeps_channels_disjoint():
    groups = [f"ch_{i // 4}" for i in range(60)]
    y = [0 if i % 5 != 0 else (1 if i % 10 == 0 else 2) for i in range(60)]
    splits = stratified_group_kfold(groups, y, n_splits=5, seed=42)
    assert len(splits) == 5
    seen_val = set()
    for tr_idx, va_idx in splits:
        tr_ch = {groups[i] for i in tr_idx}
        va_ch = {groups[i] for i in va_idx}
        assert not (tr_ch & va_ch)
        seen_val.update(va_idx)
    assert len(seen_val) == 60


def test_calibrate_safe_thresholds_protects_release_recall():
    y = [0, 0, 0, 0, 1, 1, 2, 2]
    probs = [
        [0.92, 0.05, 0.03],
        [0.85, 0.10, 0.05],
        [0.70, 0.20, 0.10],
        [0.45, 0.40, 0.15],
        [0.05, 0.90, 0.05],
        [0.08, 0.85, 0.07],
        [0.05, 0.10, 0.85],
        [0.06, 0.12, 0.82],
    ]
    res = calibrate_safe_thresholds(y, probs, min_release_recall=0.985)
    assert res["release_related_pass_recall"] == 1.0
    assert res["irrelevant_caught"] == 2
    assert res["insufficient_caught"] == 2


def test_intent_classifier_layer1_never_blocks_official_text_in_mixed_channel(monkeypatch, tmp_path):
    # Create a minimal fake IntentClassifier bypassing ORT session init
    clf = object.__new__(IntentClassifier)
    from fmr_tg_scanner.official_registry import load_official_registry

    clf.registry = load_official_registry()
    clf.t_irrelevant = 0.75
    clf.t_insufficient = 0.75
    clf.t_release_floor = 0.30
    clf.audit_sample_rate = 0.03
    clf.predict_batch = lambda msgs: [
        {"pred_label": "release_related", "probs": {"release_related": 0.94, "irrelevant": 0.04, "insufficient_context": 0.02}}
    ]

    # 1. Mixed channel with "Official" in body text MUST pass Layer 1 & reach E5 -> DeepSeek
    mixed = {
        "channel_username": "poco_f3_community",
        "msg_id": 100,
        "raw_text": "Based on LineageOS Official Trees | DerpFest Unofficial for POCO F3 (alioth)\nhttps://dl.example.com/rom.zip",
    }
    r1 = clf.route_message(mixed)
    assert r1["send_to_deepseek"] is True and r1["layer"] == "layer2_e5_onnx"

    # 2. Layer 1 blocked channel is skipped purely by channel_username metadata
    blocked = {
        "channel_username": "MIUIUpdatesTracker",
        "msg_id": 200,
        "raw_text": "HyperOS Official update for alioth",
    }
    r2 = clf.route_message(blocked)
    assert r2["send_to_deepseek"] is False and r2["action"] == "skip_channel_blocked"
