"""Tests for stratified dataset sampler (channel-disjoint train/golden_val export)."""
import json

from fmr_tg_scanner.dataset_sampler import export_annotation_datasets
from fmr_tg_scanner.evidence_store import EvidenceStore


def test_dataset_sampler_exports_channel_disjoint_stratified_jsonl(tmp_path):
    root = tmp_path / "tg-v4"
    db_path = root / "clean" / "search-v3" / "search.sqlite"
    db_path.parent.mkdir(parents=True)
    store = EvidenceStore(db_path)
    try:
        samples = [
            ("chan_a", 1, "Anyone got battery drain on latest build?", [], []),
            ("chan_a", 2, "Evolution X 10.2 Official | Android 15", ["EvoX.zip"], [{"url": "https://dl.example.com/1", "label": "Download"}]),
            ("chan_b", 3, "Coming soon! Stay tuned for HyperOS port!", [], []),
            ("chan_b", 4, "补好了", [], [{"url": "https://t.me/chan_b/2", "label": "Mirror"}]),
            ("chan_c", 5, "求一个 Redmi K40 的 TWRP 链接？", [], []),
            ("chan_d", 6, "LineageOS 22.1 Unofficial for POCO F3 (alioth)\n" + ("- fix\n" * 120), [], []),
        ]
        for ch, mid, text, att, links in samples:
            store.ingest_message(
                {
                    "channel_username": ch,
                    "msg_id": mid,
                    "text": text,
                    "attachment_names": att,
                    "link_items": links,
                },
                initial_status="uncleaned_backlog",
            )
    finally:
        store.close()

    out_dir = tmp_path / "datasets"
    summary = export_annotation_datasets(root, out_dir, train_size=4, val_size=2, seed=42)
    assert summary["channel_overlap"] == 0
    assert summary["train_exported"] >= 1
    assert summary["golden_val_exported"] >= 1

    train_rows = [json.loads(line) for line in (out_dir / "train_candidates_2000.jsonl").read_text().splitlines() if line]
    val_rows = [json.loads(line) for line in (out_dir / "golden_val_200.jsonl").read_text().splitlines() if line]
    assert train_rows and val_rows
    assert all(row["label"] is None for row in train_rows + val_rows)
    assert all(row["classifier_input"].startswith("passage:") for row in train_rows + val_rows)
