"""Phase 3 Trainer & Blind Evaluator for TG v2 E5 ONNX 3-Class Gate (runs on 103 CPU).

Strictly separates:
- Training & 5-fold StratifiedGroupKFold (by channel_username): train_candidates_2000_labeled.jsonl ONLY.
- Final blind evaluation: golden_val_200_labeled.jsonl (0 channel overlap, never used for tuning).
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import random
import time
from typing import Any

from .intent_classifier import (
    LABEL_TO_IDX,
    LABELS,
    build_e5_input,
    extract_structural_features,
)


def stratified_group_kfold(
    groups: list[str],
    y,
    n_splits: int = 5,
    seed: int = 42,
) -> list[tuple[list[int], list[int]]]:
    """Deterministic StratifiedGroupKFold assigning entire channels to folds without leakage."""
    y_list = [int(v) for v in y]
    group_indices: dict[str, list[int]] = defaultdict(list)
    for idx, g in enumerate(groups):
        group_indices[g].append(idx)

    total_counts = [0.0] * len(LABELS)
    for val in y_list:
        total_counts[val] += 1.0
    target_per_fold = [c / n_splits for c in total_counts]

    rng = random.Random(seed)
    group_items = list(group_indices.items())
    rng.shuffle(group_items)

    def _group_sort_key(item: tuple[str, list[int]]) -> tuple[int, int, int, str]:
        _, idxs = item
        cnt = [0, 0, 0]
        for idx in idxs:
            cnt[y_list[idx]] += 1
        has_ins = 1 if cnt[2] > 0 else 0
        has_irr = 1 if cnt[1] > 0 else 0
        return (-has_ins, -has_irr, -len(idxs), item[0])

    group_items.sort(key=_group_sort_key)

    fold_counts = [[0.0] * len(LABELS) for _ in range(n_splits)]
    fold_indices: list[list[int]] = [[] for _ in range(n_splits)]

    for _, idxs in group_items:
        g_cnt = [0.0] * len(LABELS)
        for idx in idxs:
            g_cnt[y_list[idx]] += 1.0
        best_fold = 0
        best_score = float("inf")
        for f in range(n_splits):
            score = 0.0
            for c in range(len(LABELS)):
                cand = fold_counts[f][c] + g_cnt[c]
                denom = max(target_per_fold[c], 1.0)
                diff = (cand - target_per_fold[c]) / denom
                score += diff * diff
            if score < best_score:
                best_score = score
                best_fold = f
        fold_indices[best_fold].extend(idxs)
        for c in range(len(LABELS)):
            fold_counts[best_fold][c] += g_cnt[c]

    splits: list[tuple[list[int], list[int]]] = []
    n_total = len(y_list)
    for f in range(n_splits):
        val_set = set(fold_indices[f])
        val_idx = sorted(val_set)
        tr_idx = [i for i in range(n_total) if i not in val_set]
        splits.append((tr_idx, val_idx))
    return splits


def _build_head_module(input_dim: int, hidden_dim: int, dropout: float = 0.30):
    import torch
    import torch.nn as nn

    if hidden_dim <= 0:
        return nn.Linear(input_dim, len(LABELS))
    return nn.Sequential(
        nn.Linear(input_dim, hidden_dim),
        nn.GELU(),
        nn.Dropout(dropout),
        nn.Linear(hidden_dim, len(LABELS)),
    )


def _train_head_torch(
    X_tr,
    y_tr,
    *,
    hidden_dim: int = 48,
    dropout: float = 0.20,
    weight_power: float = 0.55,
    lr: float = 3e-3,
    weight_decay: float = 1e-2,
    label_smoothing: float = 0.0,
    epochs: int = 140,
    seed: int = 42,
):
    import numpy as np
    import torch
    import torch.nn as nn

    torch.manual_seed(seed)
    np.random.seed(seed)

    counts = np.bincount(y_tr, minlength=len(LABELS)).astype(np.float32)
    raw_weights = (counts.sum() / (len(LABELS) * np.maximum(counts, 1.0))) ** weight_power
    weights = torch.tensor(raw_weights / raw_weights.mean(), dtype=torch.float32)

    model = _build_head_module(X_tr.shape[1], hidden_dim=hidden_dim, dropout=dropout)
    optimizer = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=weight_decay)
    criterion = nn.CrossEntropyLoss(weight=weights, label_smoothing=label_smoothing)

    xt = torch.tensor(X_tr, dtype=torch.float32)
    yt = torch.tensor(y_tr, dtype=torch.long)

    model.train()
    for _ in range(epochs):
        optimizer.zero_grad()
        logits = model(xt)
        loss = criterion(logits, yt)
        loss.backward()
        optimizer.step()

    model.eval()
    return model


def _predict_head_torch(model, X):
    import numpy as np
    import torch

    with torch.no_grad():
        logits = model(torch.tensor(X, dtype=torch.float32))
        probs = torch.softmax(logits, dim=-1).cpu().numpy()
    return probs.astype(np.float32)


def compute_classification_metrics(y_true, y_pred) -> dict[str, Any]:
    cm = [[0, 0, 0], [0, 0, 0], [0, 0, 0]]
    y_t = [int(v) for v in y_true]
    y_p = [int(v) for v in y_pred]
    for t, p in zip(y_t, y_p):
        cm[t][p] += 1

    per_class: dict[str, dict[str, Any]] = {}
    f1s: list[float] = []
    for idx, name in enumerate(LABELS):
        tp = cm[idx][idx]
        fp = sum(cm[r][idx] for r in range(len(LABELS))) - tp
        fn = sum(cm[idx]) - tp
        support = sum(cm[idx])
        prec = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        rec = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        f1 = (2 * prec * rec / (prec + rec)) if (prec + rec) > 0 else 0.0
        f1s.append(f1)
        per_class[name] = {
            "precision": round(prec, 4),
            "recall": round(rec, 4),
            "f1": round(f1, 4),
            "support": support,
        }

    acc = sum(1 for t, p in zip(y_t, y_p) if t == p) / len(y_t) if y_t else 0.0
    return {
        "accuracy": round(acc, 4),
        "macro_f1": round(sum(f1s) / len(f1s), 4),
        "per_class": per_class,
        "confusion_matrix_labels": list(LABELS),
        "confusion_matrix": cm,
    }


def evaluate_gate_thresholds(
    y_true,
    probs,
    *,
    t_irrelevant: float,
    t_insufficient: float,
    t_release_floor: float,
) -> dict[str, Any]:
    """Compute routing metrics under the Layer 2 gate thresholds."""
    y_list = [int(v) for v in y_true]
    rel_total = rel_kept = irr_total = irr_caught = ins_total = ins_caught = 0
    intercepted_total = 0

    for label, row in zip(y_list, probs):
        p_rel, p_irr, p_ins = float(row[0]), float(row[1]), float(row[2])
        skip_irr = (p_irr >= t_irrelevant) and (p_rel < t_release_floor)
        drop_ins = (not skip_irr) and (p_ins >= t_insufficient) and (p_rel < t_release_floor)
        intercepted = skip_irr or drop_ins
        if intercepted:
            intercepted_total += 1
        if label == 0:
            rel_total += 1
            if not intercepted:
                rel_kept += 1
        elif label == 1:
            irr_total += 1
            if intercepted:
                irr_caught += 1
        else:
            ins_total += 1
            if intercepted:
                ins_caught += 1

    rel_recall = rel_kept / rel_total if rel_total else 1.0
    rel_fn = rel_total - rel_kept
    non_rel_total = irr_total + ins_total
    non_rel_caught = irr_caught + ins_caught

    return {
        "t_irrelevant": round(float(t_irrelevant), 3),
        "t_insufficient": round(float(t_insufficient), 3),
        "t_release_floor": round(float(t_release_floor), 3),
        "release_related_total": rel_total,
        "release_related_passed": rel_kept,
        "release_related_false_negatives": rel_fn,
        "release_related_pass_recall": round(rel_recall, 4),
        "irrelevant_caught": irr_caught,
        "irrelevant_total": irr_total,
        "irrelevant_intercept_rate": round(irr_caught / irr_total if irr_total else 0.0, 4),
        "insufficient_caught": ins_caught,
        "insufficient_total": ins_total,
        "insufficient_intercept_rate": round(ins_caught / ins_total if ins_total else 0.0, 4),
        "non_release_intercepted": non_rel_caught,
        "non_release_total": non_rel_total,
        "non_release_intercept_rate": round(non_rel_caught / non_rel_total if non_rel_total else 0.0, 4),
        "llm_call_reduction_rate": round(intercepted_total / len(y_list) if y_list else 0.0, 4),
    }


def calibrate_safe_thresholds(
    y_true,
    probs,
    *,
    min_release_recall: float = 0.990,
    min_t_irrelevant: float = 0.75,
    min_t_insufficient: float = 0.65,
    max_t_release_floor: float = 0.25,
) -> dict[str, Any]:
    """Grid-search high-safety thresholds strictly on OOF predictions to protect release_related recall."""
    best: dict[str, Any] | None = None
    best_score = -1.0

    t_rel_grid = [v for v in (0.10, 0.15, 0.20, 0.25) if v <= max_t_release_floor]
    t_irr_grid = [v for v in (0.70, 0.75, 0.80, 0.85, 0.90) if v >= min_t_irrelevant]
    t_ins_grid = [v for v in (0.60, 0.65, 0.70, 0.75, 0.80, 0.85) if v >= min_t_insufficient]

    for t_rel in t_rel_grid:
        for t_irr in t_irr_grid:
            for t_ins in t_ins_grid:
                metrics = evaluate_gate_thresholds(
                    y_true,
                    probs,
                    t_irrelevant=t_irr,
                    t_insufficient=t_ins,
                    t_release_floor=t_rel,
                )
                if metrics["release_related_pass_recall"] < min_release_recall:
                    continue
                score = metrics["non_release_intercept_rate"] + 0.05 * metrics["release_related_pass_recall"]
                if score > best_score:
                    best_score = score
                    best = metrics

    if best is None:
        if min_release_recall > 0.975:
            return calibrate_safe_thresholds(
                y_true,
                probs,
                min_release_recall=0.975,
                min_t_irrelevant=min_t_irrelevant,
                min_t_insufficient=min_t_insufficient,
                max_t_release_floor=max_t_release_floor,
            )
        return evaluate_gate_thresholds(
            y_true,
            probs,
            t_irrelevant=max(min_t_irrelevant, 0.85),
            t_insufficient=max(min_t_insufficient, 0.75),
            t_release_floor=min(max_t_release_floor, 0.15),
        )
    return best


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _encode_rows_on_103(rows: list[dict[str, Any]], model_dir: Path, *, max_length: int = 384):
    import numpy as np
    import onnxruntime as ort
    from transformers import AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(str(model_dir / "tokenizer"))
    opts = ort.SessionOptions()
    opts.intra_op_num_threads = 4
    opts.inter_op_num_threads = 1
    session = ort.InferenceSession(
        str(model_dir / "e5_small_int8.onnx"),
        sess_options=opts,
        providers=["CPUExecutionProvider"],
    )

    texts = [build_e5_input(r) for r in rows]
    embs = []
    for i in range(0, len(texts), 32):
        batch = texts[i : i + 32]
        enc = tokenizer(batch, padding=True, truncation=True, max_length=max_length, return_tensors="np")
        out = session.run(
            None,
            {
                "input_ids": enc["input_ids"].astype(np.int64),
                "attention_mask": enc["attention_mask"].astype(np.int64),
            },
        )[0]
        embs.append(out.astype(np.float32))
    return np.vstack(embs)


def train_and_evaluate(
    *,
    train_jsonl: str | Path,
    golden_val_jsonl: str | Path,
    model_dir: str | Path = "/var/lib/findmyrom/models/tg-intent-v2",
    min_release_recall: float = 0.985,
) -> dict[str, Any]:
    import numpy as np
    import onnxruntime as ort
    import torch
    import torch.nn as nn

    model_root = Path(model_dir)
    train_rows = _load_jsonl(Path(train_jsonl))
    val_rows = _load_jsonl(Path(golden_val_jsonl))

    train_channels = {r["channel_username"] for r in train_rows}
    val_channels = {r["channel_username"] for r in val_rows}
    if train_channels & val_channels:
        raise ValueError(f"Channel leakage detected between train and golden_val: {train_channels & val_channels}")

    y_train = np.array([LABEL_TO_IDX[r["label"]] for r in train_rows], dtype=np.int64)
    y_val = np.array([LABEL_TO_IDX[r["label"]] for r in val_rows], dtype=np.int64)
    groups_train = [r["channel_username"] for r in train_rows]

    max_length = 384
    struct_scale = 0.25

    t0 = time.perf_counter()
    import hashlib
    cache_key = hashlib.sha256(
        (Path(train_jsonl).read_bytes() + b"|" + Path(golden_val_jsonl).read_bytes() + f"|{max_length}|headtail".encode()).strip()
    ).hexdigest()[:16]
    emb_cache_path = model_root / f"_emb_cache_{cache_key}.npz"
    if emb_cache_path.is_file():
        cached = np.load(emb_cache_path)
        emb_train = cached["emb_train"]
        emb_val = cached["emb_val"]
    else:
        emb_train = _encode_rows_on_103(train_rows, model_root, max_length=max_length)
        emb_val = _encode_rows_on_103(val_rows, model_root, max_length=max_length)
        np.savez(emb_cache_path, emb_train=emb_train, emb_val=emb_val)
    encode_seconds = round(time.perf_counter() - t0, 2)

    struct_train = np.array([extract_structural_features(r) for r in train_rows], dtype=np.float32) * struct_scale
    struct_val = np.array([extract_structural_features(r) for r in val_rows], dtype=np.float32) * struct_scale

    X_train_e5 = emb_train
    X_val_e5 = emb_val
    X_train_both = np.hstack([emb_train, struct_train]).astype(np.float32)
    X_val_both = np.hstack([emb_val, struct_val]).astype(np.float32)

    splits = stratified_group_kfold(groups_train, y_train, n_splits=5, seed=42)

    candidates = [
        {"name": "e5_only_linear", "use_struct": False, "hidden_dim": 0, "dropout": 0.0, "weight_power": 0.55, "lr": 5e-3, "weight_decay": 1e-2, "epochs": 140},
        {"name": "e5_only_mlp48", "use_struct": False, "hidden_dim": 48, "dropout": 0.20, "weight_power": 0.55, "lr": 3e-3, "weight_decay": 1e-2, "epochs": 140},
        {"name": "e5_plus_struct_linear", "use_struct": True, "hidden_dim": 0, "dropout": 0.0, "weight_power": 0.55, "lr": 5e-3, "weight_decay": 1e-2, "epochs": 140},
        {"name": "e5_plus_struct_mlp48", "use_struct": True, "hidden_dim": 48, "dropout": 0.20, "weight_power": 0.55, "lr": 3e-3, "weight_decay": 1e-2, "epochs": 140},
        {"name": "e5_plus_struct_mlp64", "use_struct": True, "hidden_dim": 64, "dropout": 0.20, "weight_power": 0.55, "lr": 3e-3, "weight_decay": 1e-2, "epochs": 140},
    ]

    oof_results = []
    for cfg in candidates:
        X_tr_all = X_train_both if cfg["use_struct"] else X_train_e5
        oof_probs = np.zeros((len(train_rows), len(LABELS)), dtype=np.float32)
        in_fold_train_accs = []
        in_fold_train_f1s = []
        for tr_idx, va_idx in splits:
            m = _train_head_torch(
                X_tr_all[tr_idx],
                y_train[tr_idx],
                hidden_dim=cfg["hidden_dim"],
                dropout=cfg["dropout"],
                weight_power=cfg["weight_power"],
                lr=cfg["lr"],
                weight_decay=cfg["weight_decay"],
                epochs=cfg["epochs"],
            )
            tr_probs = _predict_head_torch(m, X_tr_all[tr_idx])
            tr_m = compute_classification_metrics(y_train[tr_idx], np.argmax(tr_probs, axis=1))
            in_fold_train_accs.append(tr_m["accuracy"])
            in_fold_train_f1s.append(tr_m["macro_f1"])
            oof_probs[va_idx] = _predict_head_torch(m, X_tr_all[va_idx])
        cls_metrics = compute_classification_metrics(y_train, np.argmax(oof_probs, axis=1))
        gate_metrics = calibrate_safe_thresholds(
            y_train,
            oof_probs,
            min_release_recall=min_release_recall,
            min_t_irrelevant=0.75,
            min_t_insufficient=0.65,
            max_t_release_floor=0.25,
        )
        mean_tr_acc = float(np.mean(in_fold_train_accs))
        mean_tr_f1 = float(np.mean(in_fold_train_f1s))
        oof_results.append(
            {
                **cfg,
                "train_vs_oof_gap": {
                    "mean_train_acc": round(mean_tr_acc, 4),
                    "oof_acc": cls_metrics["accuracy"],
                    "acc_gap": round(mean_tr_acc - cls_metrics["accuracy"], 4),
                    "mean_train_macro_f1": round(mean_tr_f1, 4),
                    "oof_macro_f1": cls_metrics["macro_f1"],
                    "macro_f1_gap": round(mean_tr_f1 - cls_metrics["macro_f1"], 4),
                },
                "oof_classification": cls_metrics,
                "oof_gate": gate_metrics,
            }
        )

    # Choose best candidate: prioritize release_related recall >= min_release_recall, low overfitting gap, and non_release interception
    def _rank_key(item: dict[str, Any]) -> tuple[float, float, float]:
        gate = item["oof_gate"]
        cls_m = item["oof_classification"]
        gap_penalty = max(0.0, item["train_vs_oof_gap"]["macro_f1_gap"] - 0.08)
        return (
            1.0 if gate["release_related_pass_recall"] >= min_release_recall else 0.0,
            gate["non_release_intercept_rate"] + 0.30 * cls_m["macro_f1"] - 0.5 * gap_penalty,
            cls_m["per_class"]["insufficient_context"]["f1"],
        )

    best_cfg = max(oof_results, key=_rank_key)
    use_struct = best_cfg["use_struct"]
    X_full_train = X_train_both if use_struct else X_train_e5
    X_full_val = X_val_both if use_struct else X_val_e5

    final_head = _train_head_torch(
        X_full_train,
        y_train,
        hidden_dim=best_cfg["hidden_dim"],
        dropout=best_cfg["dropout"],
        weight_power=best_cfg["weight_power"],
        lr=best_cfg["lr"],
        weight_decay=best_cfg["weight_decay"],
        epochs=best_cfg["epochs"],
    )

    class HeadWithSoftmax(nn.Module):
        def __init__(self, net: nn.Module) -> None:
            super().__init__()
            self.net = net

        def forward(self, features: torch.Tensor) -> torch.Tensor:
            return torch.softmax(self.net(features), dim=-1)

    export_net = HeadWithSoftmax(final_head)
    export_net.eval()

    head_onnx_path = model_root / "intent_head.onnx"
    dummy_in = torch.zeros((1, X_full_train.shape[1]), dtype=torch.float32)
    with torch.no_grad():
        torch.onnx.export(
            export_net,
            dummy_in,
            str(head_onnx_path),
            input_names=["features"],
            output_names=["probs"],
            dynamic_axes={"features": {0: "batch"}, "probs": {0: "batch"}},
            opset_version=17,
            dynamo=False,
        )

    # Verify PyTorch vs ONNX numerical parity
    torch_val_probs = _predict_head_torch(final_head, X_full_val)
    head_sess = ort.InferenceSession(str(head_onnx_path), providers=["CPUExecutionProvider"])
    onnx_val_probs = head_sess.run(None, {"features": X_full_val.astype(np.float32)})[0]
    max_abs_diff = float(np.max(np.abs(torch_val_probs - onnx_val_probs)))

    # Save threshold config
    gate_cfg = best_cfg["oof_gate"]
    threshold_config = {
        "model_version": "tg-intent-head-v2.1",
        "selected_candidate": best_cfg["name"],
        "use_structural_features": use_struct,
        "struct_scale": struct_scale,
        "feature_dim": int(X_full_train.shape[1]),
        "labels": list(LABELS),
        "t_irrelevant": gate_cfg["t_irrelevant"],
        "t_insufficient": gate_cfg["t_insufficient"],
        "t_release_floor": gate_cfg["t_release_floor"],
        "irrelevant_audit_sample_rate": 0.03,
        "max_length": max_length,
    }
    (model_root / "threshold_config.json").write_text(
        json.dumps(threshold_config, indent=2) + "\n", encoding="utf-8"
    )

    # One-shot blind evaluation on golden_val_200_labeled.jsonl using IntentClassifier
    from .intent_classifier import IntentClassifier

    clf = IntentClassifier(model_dir=model_root)
    t_eval = time.perf_counter()
    routes = [clf.route_message(r) for r in val_rows]
    eval_ms_per_msg = (time.perf_counter() - t_eval) * 1000.0 / len(val_rows)

    golden_cls = compute_classification_metrics(y_val, np.argmax(onnx_val_probs, axis=1))
    golden_gate = evaluate_gate_thresholds(
        y_val,
        onnx_val_probs,
        t_irrelevant=gate_cfg["t_irrelevant"],
        t_insufficient=gate_cfg["t_insufficient"],
        t_release_floor=gate_cfg["t_release_floor"],
    )

    # Also measure end-to-end route_message decisions (includes Layer 1 channel blocklist & basic hard rules)
    routed_sent = np.array([r["send_to_deepseek"] for r in routes], dtype=bool)
    routed_audit = np.array([r["audit_sampled"] for r in routes], dtype=bool)
    routed_non_audit_skip = (~routed_sent) & (~routed_audit)

    rel_mask = y_val == 0
    fn_examples = []
    for idx, r in enumerate(val_rows):
        if rel_mask[idx] and not routed_sent[idx]:
            fn_examples.append(
                {
                    "sample_id": r["sample_id"],
                    "channel_username": r["channel_username"],
                    "layer": routes[idx]["layer"],
                    "action": routes[idx]["action"],
                    "reason": routes[idx]["reason"],
                    "probs": routes[idx]["probs"],
                    "raw_text_snippet": (r.get("raw_text") or "")[:200],
                    "attachment_names": r.get("attachment_names"),
                    "link_items": r.get("link_items"),
                }
            )

    report = {
        "train_count": len(train_rows),
        "golden_val_count": len(val_rows),
        "channel_overlap": len(train_channels & val_channels),
        "encode_seconds_total": encode_seconds,
        "oof_candidates": oof_results,
        "selected_candidate": best_cfg["name"],
        "threshold_config": threshold_config,
        "pytorch_vs_onnx_max_abs_diff": round(max_abs_diff, 8),
        "golden_val_3class_metrics": golden_cls,
        "golden_val_gate_metrics_pure_e5": golden_gate,
        "golden_val_end_to_end_routing": {
            "release_related_passed": int((rel_mask & routed_sent).sum()),
            "release_related_total": int(rel_mask.sum()),
            "release_related_recall": round(float((rel_mask & routed_sent).sum() / max(1, rel_mask.sum())), 4),
            "irrelevant_skipped_before_audit": int(((y_val == 1) & (routed_non_audit_skip | routed_audit)).sum()),
            "irrelevant_total": int((y_val == 1).sum()),
            "insufficient_dropped": int(((y_val == 2) & routed_non_audit_skip).sum()),
            "insufficient_total": int((y_val == 2).sum()),
            "route_action_counts": dict(Counter(r["action"] for r in routes)),
            "false_negative_examples": fn_examples,
        },
        "avg_end_to_end_ms_per_message": round(eval_ms_per_msg, 2),
    }
    (model_root / "eval_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--train-jsonl", required=True)
    parser.add_argument("--golden-val-jsonl", required=True)
    parser.add_argument("--model-dir", default="/var/lib/findmyrom/models/tg-intent-v2")
    parser.add_argument("--min-release-recall", type=float, default=0.985)
    args = parser.parse_args(argv)
    report = train_and_evaluate(
        train_jsonl=args.train_jsonl,
        golden_val_jsonl=args.golden_val_jsonl,
        model_dir=args.model_dir,
        min_release_recall=args.min_release_recall,
    )
    summary = {k: v for k, v in report.items() if k != "oof_candidates"}
    summary["oof_comparison"] = [
        {
            "name": c["name"],
            "macro_f1": c["oof_classification"]["macro_f1"],
            "ins_f1": c["oof_classification"]["per_class"]["insufficient_context"]["f1"],
            "irr_f1": c["oof_classification"]["per_class"]["irrelevant"]["f1"],
            "rel_pass_recall": c["oof_gate"]["release_related_pass_recall"],
            "non_rel_intercept_rate": c["oof_gate"]["non_release_intercept_rate"],
        }
        for c in report["oof_candidates"]
    ]
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
