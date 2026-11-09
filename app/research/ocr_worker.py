from __future__ import annotations

import json
import sys

from paddleocr import PaddleOCR


def main() -> None:
    payload = json.load(sys.stdin)
    models = payload["model_dir"]
    ocr = PaddleOCR(
        text_detection_model_name="PP-OCRv5_mobile_det",
        text_detection_model_dir=f"{models}/PP-OCRv5_mobile_det",
        text_recognition_model_name="PP-OCRv5_mobile_rec",
        text_recognition_model_dir=f"{models}/PP-OCRv5_mobile_rec",
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=False,
        device="cpu",
    )
    result = ocr.predict(payload["image"])
    texts = list(result[0]["rec_texts"]) if result else []
    scores = [float(value) for value in result[0].get("rec_scores", [])] if result else []
    boxes = result[0].get("rec_boxes", []) if result else []
    boxes = [[float(n) for n in box] for box in boxes]
    print(json.dumps({"texts": texts, "scores": scores, "boxes": boxes, "model": "PP-OCRv5_mobile"}))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}))
