#!/usr/bin/env python3
"""Setup intfloat/multilingual-e5-small ONNX INT8 model environment strictly on 103 CPU.

Never run this on a developer laptop. Downloads HuggingFace weights, exports ONNX,
applies dynamic INT8 quantization, and runs an end-to-end CPU latency benchmark on 103.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import time


def setup_e5_onnx_int8(
    model_dir: str | Path,
    *,
    model_id: str = "intfloat/multilingual-e5-small",
) -> dict[str, object]:
    import numpy as np
    import onnx
    import onnxruntime as ort
    from onnxruntime.quantization import QuantType, quantize_dynamic
    import torch
    from transformers import AutoModel, AutoTokenizer

    out = Path(model_dir)
    out.mkdir(parents=True, exist_ok=True)
    fp32_path = out / "e5_small_fp32.onnx"
    int8_path = out / "e5_small_int8.onnx"
    tok_dir = out / "tokenizer"

    tokenizer = AutoTokenizer.from_pretrained(model_id)
    tokenizer.save_pretrained(str(tok_dir))

    if not int8_path.is_file():
        model = AutoModel.from_pretrained(model_id)
        model.eval()

        class MeanPooledE5(torch.nn.Module):
            def __init__(self, encoder: torch.nn.Module) -> None:
                super().__init__()
                self.encoder = encoder

            def forward(
                self,
                input_ids: torch.Tensor,
                attention_mask: torch.Tensor,
            ) -> torch.Tensor:
                outputs = self.encoder(input_ids=input_ids, attention_mask=attention_mask)
                last_hidden = outputs.last_hidden_state
                mask = attention_mask.unsqueeze(-1).expand(last_hidden.size()).float()
                summed = torch.sum(last_hidden * mask, dim=1)
                counts = torch.clamp(mask.sum(dim=1), min=1e-9)
                pooled = summed / counts
                return torch.nn.functional.normalize(pooled, p=2, dim=1)

        wrapper = MeanPooledE5(model)
        sample = tokenizer(
            ["passage: LineageOS 22.1 Unofficial for POCO F3 (alioth)"],
            padding=True,
            truncation=True,
            max_length=256,
            return_tensors="pt",
        )
        with torch.no_grad():
            torch.onnx.export(
                wrapper,
                (sample["input_ids"], sample["attention_mask"]),
                str(fp32_path),
                input_names=["input_ids", "attention_mask"],
                output_names=["embedding"],
                dynamic_axes={
                    "input_ids": {0: "batch", 1: "seq"},
                    "attention_mask": {0: "batch", 1: "seq"},
                    "embedding": {0: "batch"},
                },
                opset_version=17,
                dynamo=False,
            )

        # Ensure single-file self-contained model before quantization if external data was emitted
        onnx_model = onnx.load(str(fp32_path), load_external_data=True)
        onnx.save_model(onnx_model, str(fp32_path), save_as_external_data=False)

        quantize_dynamic(
            model_input=str(fp32_path),
            model_output=str(int8_path),
            weight_type=QuantType.QInt8,
        )
        if fp32_path.is_file():
            fp32_path.unlink()

    opts = ort.SessionOptions()
    opts.intra_op_num_threads = 4
    opts.inter_op_num_threads = 1
    session = ort.InferenceSession(str(int8_path), sess_options=opts, providers=["CPUExecutionProvider"])

    texts = [
        "passage: Evolution X 10.2 Unofficial | Android 15 | POCO F3 (alioth) [ATTACHMENTS] EvoX.zip",
        "passage: Anyone got battery drain on latest crDroid for alioth?",
    ]
    enc = tokenizer(texts, padding=True, truncation=True, max_length=256, return_tensors="np")
    inputs = {
        "input_ids": enc["input_ids"].astype(np.int64),
        "attention_mask": enc["attention_mask"].astype(np.int64),
    }

    # Warmup + benchmark
    for _ in range(3):
        session.run(None, inputs)
    t0 = time.perf_counter()
    iters = 20
    for _ in range(iters):
        emb = session.run(None, inputs)[0]
    elapsed_ms = (time.perf_counter() - t0) * 1000.0 / (iters * len(texts))

    meta = {
        "model_id": model_id,
        "int8_onnx_path": str(int8_path),
        "int8_size_mb": round(int8_path.stat().st_size / (1024 * 1024), 2),
        "tokenizer_dir": str(tok_dir),
        "embedding_dim": int(emb.shape[1]),
        "avg_ms_per_message": round(elapsed_ms, 2),
    }
    (out / "encoder_meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    return meta


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model-dir",
        default="/var/lib/findmyrom/models/tg-intent-v2",
        help="Target directory on 103 for E5 ONNX INT8 model & tokenizer",
    )
    args = parser.parse_args()
    result = setup_e5_onnx_int8(args.model_dir)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
