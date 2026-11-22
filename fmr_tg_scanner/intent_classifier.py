"""TG Pipeline v2 Layer 1 + Layer 2 (multilingual-e5-small ONNX INT8) Intent Classifier.

Strictly enforces the Three-Question Separation & Phase 3 Layer Boundaries:
- Layer 1 (pre-E5): Pure metadata checks ONLY (channel blocklist from config/official_sources.yaml
  + basic empty/trivial length hard filter with 5% blindspot retention).
  FORBIDDEN: No Official/Unofficial regex on body text or titles before E5!
- Layer 2 (E5 ONNX INT8): 3-class semantic gate:
  - release_related -> pass to DeepSeek
  - insufficient_context (above safe threshold & release_related below floor) -> explicit drop
  - irrelevant (above safe threshold T & release_related below floor) -> skip DeepSeek
    (with 2%~5% deterministic/random audit sampling sent to DeepSeek anyway)
  - ambiguous / borderline -> always pass through to DeepSeek.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlsplit

from .dataset_sampler import format_classifier_input
from .evidence_store import evaluate_basic_hard_rules, is_blindspot_sample
from .official_registry import OfficialSourcesRegistry, load_official_registry

LABELS = ("release_related", "irrelevant", "insufficient_context")
LABEL_TO_IDX = {name: i for i, name in enumerate(LABELS)}
STRUCTURAL_FEATURE_NAMES = (
    "has_attachment",
    "has_external_link",
    "has_tg_link",
    "has_reply",
    "has_forward",
    "is_bare_url_only",
    "is_empty_text",
    "has_version_digit",
    "log_text_len",
    "norm_line_count",
    "has_forum_link_only",
    "no_download_and_no_version",
)

# Purely numeric/version structure (NEVER Official/Unofficial regex!)
_VERSION_DIGIT_RE = re.compile(
    r"(?:\bv?\d+\.\d+(?:\.\d+)*\b|\b(?:OS|V|R|A)\d+(?:\.\d+)*\b)",
    re.I,
)
_BARE_URL_RE = re.compile(r"^https?://\S+$", re.I)


def extract_structural_features(message: dict[str, Any]) -> list[float]:
    """Extract 12 pure structural metadata features (zero Official/Unofficial text regex)."""
    text = (message.get("raw_text") if "raw_text" in message else message.get("text")) or ""
    stripped = text.strip()
    attachments = list(message.get("attachment_names") or [])
    if message.get("file_attachment_name") and message["file_attachment_name"] not in attachments:
        attachments.append(message["file_attachment_name"])
    attachments = [str(a).strip() for a in attachments if str(a).strip()]

    raw_links: list[str] = []
    for item in message.get("link_items") or []:
        if isinstance(item, dict) and item.get("url"):
            raw_links.append(str(item["url"]).strip())
    for u in message.get("links") or []:
        if str(u).strip() and str(u).strip() not in raw_links:
            raw_links.append(str(u).strip())

    tg_links = []
    ext_links = []
    forum_links = []
    for u in raw_links:
        try:
            host = (urlsplit(u).hostname or "").lower()
        except ValueError:
            continue
        if host in ("t.me", "telegram.me", "telegram.dog"):
            tg_links.append(u)
        elif host:
            ext_links.append(u)
            if any(f in host for f in ("xdaforums.com", "forum.xda-developers.com")):
                forum_links.append(u)

    reply_ctx = message.get("reply_context") or {}
    has_reply = bool(
        message.get("reply_to_msg_id")
        or message.get("reply_to_post_url")
        or reply_ctx.get("reply_to_msg_id")
        or reply_ctx.get("reply_to_post_url")
        or reply_ctx.get("reply_text")
    )
    fwd_ctx = message.get("forward_context") or {}
    has_forward = bool(
        message.get("has_forward_header")
        or message.get("forwarded_from_url")
        or fwd_ctx.get("has_forward_header")
        or fwd_ctx.get("forwarded_from_url")
    )

    has_attachment = 1.0 if attachments else 0.0
    has_external_link = 1.0 if ext_links else 0.0
    has_tg_link = 1.0 if tg_links else 0.0
    is_bare_url = 1.0 if (stripped and _BARE_URL_RE.fullmatch(stripped)) else 0.0
    is_empty = 1.0 if not stripped else 0.0
    combined_for_ver = stripped + " " + " ".join(attachments)
    has_version_digit = 1.0 if _VERSION_DIGIT_RE.search(combined_for_ver) else 0.0
    log_text_len = min(1.0, math.log1p(len(stripped)) / 8.0)
    num_lines = len([line for line in stripped.splitlines() if line.strip()])
    norm_line_count = min(1.0, num_lines / 20.0)
    has_forum_link_only = 1.0 if (ext_links and len(forum_links) == len(ext_links) and not attachments) else 0.0
    no_download_and_no_version = 1.0 if (not has_attachment and not has_external_link and not has_version_digit) else 0.0

    return [
        has_attachment,
        has_external_link,
        has_tg_link,
        1.0 if has_reply else 0.0,
        1.0 if has_forward else 0.0,
        is_bare_url,
        is_empty,
        has_version_digit,
        log_text_len,
        norm_line_count,
        has_forum_link_only,
        no_download_and_no_version,
    ]


def _is_irrelevant_audit_sample(post_key: str, rate: float = 0.03) -> bool:
    """Deterministic ~3% (within 2%-5%) random audit sample for skipped irrelevant posts."""
    if rate <= 0:
        return False
    digest = hashlib.sha256(f"audit:{post_key}".encode("utf-8")).digest()
    val = int.from_bytes(digest[:4], "big") / 2**32
    return val < rate


class IntentClassifier:
    """103 CPU ONNX INT8 multilingual-e5-small + 3-class head inference gate."""

    def __init__(
        self,
        model_dir: str | Path = "/var/lib/findmyrom/models/tg-intent-v2",
        *,
        registry: OfficialSourcesRegistry | None = None,
        intra_op_threads: int = 4,
    ) -> None:
        import onnxruntime as ort
        from transformers import AutoTokenizer

        self.model_dir = Path(model_dir)
        self.registry = registry or load_official_registry()

        encoder_path = self.model_dir / "e5_small_int8.onnx"
        head_path = self.model_dir / "intent_head.onnx"
        tok_dir = self.model_dir / "tokenizer"
        cfg_path = self.model_dir / "threshold_config.json"

        self.config = json.loads(cfg_path.read_text(encoding="utf-8"))
        self.use_structural_features = bool(self.config.get("use_structural_features", False))
        self.t_irrelevant = float(self.config["t_irrelevant"])
        self.t_insufficient = float(self.config["t_insufficient"])
        self.t_release_floor = float(self.config["t_release_floor"])
        self.audit_sample_rate = float(self.config.get("irrelevant_audit_sample_rate", 0.03))
        self.max_length = int(self.config.get("max_length", 256))

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = intra_op_threads
        opts.inter_op_num_threads = 1

        self.tokenizer = AutoTokenizer.from_pretrained(str(tok_dir))
        self.encoder_session = ort.InferenceSession(
            str(encoder_path), sess_options=opts, providers=["CPUExecutionProvider"]
        )
        self.head_session = ort.InferenceSession(
            str(head_path), sess_options=opts, providers=["CPUExecutionProvider"]
        )

    def encode_texts(self, texts: list[str], *, batch_size: int = 32):
        import numpy as np

        chunks = []
        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]
            enc = self.tokenizer(
                batch,
                padding=True,
                truncation=True,
                max_length=self.max_length,
                return_tensors="np",
            )
            emb = self.encoder_session.run(
                None,
                {
                    "input_ids": enc["input_ids"].astype(np.int64),
                    "attention_mask": enc["attention_mask"].astype(np.int64),
                },
            )[0]
            chunks.append(emb.astype(np.float32))
        return np.vstack(chunks) if chunks else np.zeros((0, 384), dtype=np.float32)

    def predict_probs_from_features(self, features):
        import numpy as np

        input_name = self.head_session.get_inputs()[0].name
        probs = self.head_session.run(None, {input_name: features.astype(np.float32)})[0]
        return probs

    def predict_batch(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        import numpy as np

        if not messages:
            return []
        formatted = [
            m.get("classifier_input")
            or format_classifier_input(
                m.get("raw_text") if "raw_text" in m else (m.get("text") or ""),
                m.get("attachment_names"),
                m.get("link_items"),
                m.get("reply_context"),
            )
            for m in messages
        ]
        embs = self.encode_texts(formatted)
        if self.use_structural_features:
            struct = np.array([extract_structural_features(m) for m in messages], dtype=np.float32)
            feats = np.hstack([embs, struct]).astype(np.float32)
        else:
            feats = embs
        probs = self.predict_probs_from_features(feats)
        results = []
        for idx, p in enumerate(probs):
            p_rel, p_irr, p_ins = float(p[0]), float(p[1]), float(p[2])
            pred_label = LABELS[int(np.argmax(p))]
            results.append(
                {
                    "pred_label": pred_label,
                    "probs": {
                        "release_related": round(p_rel, 6),
                        "irrelevant": round(p_irr, 6),
                        "insufficient_context": round(p_ins, 6),
                    },
                }
            )
        return results

    def route_message(self, message: dict[str, Any]) -> dict[str, Any]:
        """Apply Layer 1 (pure metadata only) + Layer 2 (E5 ONNX INT8) routing decision."""
        channel = str(message.get("channel_username") or "").strip().lower().lstrip("@")
        msg_id = message.get("msg_id")
        post_key = f"{channel}|{msg_id}"

        # Layer 1a: Exact lowercase channel blocklist from official_sources.yaml
        blocked, block_info = self.registry.is_layer1_blocked_channel(channel)
        if blocked:
            return {
                "layer": "layer1_channel_blocklist",
                "action": "skip_channel_blocked",
                "send_to_deepseek": False,
                "reason": f"layer1_blocked_channel:{block_info.get('category')}",
                "blindspot_retained": False,
                "audit_sampled": False,
                "probs": None,
            }

        # Layer 1b: Basic empty/short hard filter (NO Official/Unofficial regex!)
        normalized_msg = {
            **message,
            "text": message.get("raw_text") if "raw_text" in message else (message.get("text") or ""),
        }
        passed_hard, hard_reason = evaluate_basic_hard_rules(normalized_msg)
        if not passed_hard:
            blindspot = is_blindspot_sample(post_key)
            return {
                "layer": "layer1_basic_hard_rules",
                "action": "blindspot_retained" if blindspot else "hard_filtered",
                "send_to_deepseek": False,
                "reason": hard_reason,
                "blindspot_retained": blindspot,
                "audit_sampled": False,
                "probs": None,
            }

        # Layer 2: Local CPU ONNX INT8 3-class semantic gate
        pred = self.predict_batch([message])[0]
        probs = pred["probs"]
        p_rel = probs["release_related"]
        p_irr = probs["irrelevant"]
        p_ins = probs["insufficient_context"]

        if p_irr >= self.t_irrelevant and p_rel < self.t_release_floor:
            audit_sampled = _is_irrelevant_audit_sample(post_key, self.audit_sample_rate)
            return {
                "layer": "layer2_e5_onnx",
                "action": "audit_sample_to_deepseek" if audit_sampled else "skip_irrelevant",
                "send_to_deepseek": audit_sampled,
                "reason": "e5_irrelevant_audit_sample" if audit_sampled else "e5_irrelevant_high_confidence",
                "blindspot_retained": False,
                "audit_sampled": audit_sampled,
                "probs": probs,
                "pred_label": pred["pred_label"],
            }

        if p_ins >= self.t_insufficient and p_rel < self.t_release_floor:
            return {
                "layer": "layer2_e5_onnx",
                "action": "drop_insufficient_context",
                "send_to_deepseek": False,
                "reason": "e5_insufficient_context",
                "blindspot_retained": False,
                "audit_sampled": False,
                "probs": probs,
                "pred_label": pred["pred_label"],
            }

        return {
            "layer": "layer2_e5_onnx",
            "action": "send_to_deepseek",
            "send_to_deepseek": True,
            "reason": "e5_release_or_ambiguous",
            "blindspot_retained": False,
            "audit_sampled": False,
            "probs": probs,
            "pred_label": pred["pred_label"],
        }
