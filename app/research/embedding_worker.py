from __future__ import annotations

import json
import sys

import numpy as np
from tokenizers import Tokenizer
import onnxruntime as ort


def main() -> None:
    payload = json.load(sys.stdin)
    texts = payload["texts"]
    if not isinstance(texts, list) or len(texts) > 256 or any(not isinstance(text, str) for text in texts):
        raise ValueError("texts must be a list of at most 256 strings")
    model_dir = payload["model_dir"]
    tokenizer = Tokenizer.from_file(f"{model_dir}/tokenizer.json")
    tokenizer.enable_truncation(max_length=512)
    session = ort.InferenceSession(f"{model_dir}/model.onnx", providers=["CPUExecutionProvider"])
    vectors = []
    for text in texts:
        encoded = tokenizer.encode(text)
        ids = np.array([encoded.ids], dtype=np.int64)
        mask = np.array([encoded.attention_mask], dtype=np.int64)
        output = session.run(["sentence_embedding"], {"input_ids": ids, "attention_mask": mask})[0][0]
        vector = output / (np.linalg.norm(output) + 1e-12)
        vectors.append([float(value) for value in vector])
    print(json.dumps({"vectors": vectors, "model": "BAAI/bge-m3:onnx", "dimensions": 1024}))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}))
