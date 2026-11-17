from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path
from typing import Any

from app.gateway import providers
from app.gateway.config import ModelSpec, get_config

OCR_MODEL = "deepseek-ocr"
OCR_PURPOSE = "text_recognition"
_MARKER = re.compile(
    r"<｜ref｜>.*?<｜/ref｜>\s*<｜box｜>\s*\[\[\s*"
    r"([0-9.]+)\s*,\s*([0-9.]+)\s*,\s*([0-9.]+)\s*,\s*([0-9.]+)"
    r"\s*\]\]\s*<｜/box｜>",
    re.S,
)
_TAGS = re.compile(r"<｜[^｜]+｜>")


def ocr_spec() -> ModelSpec:
    configured = next((spec for spec in get_config().models.values()
                       if spec.vendor == "deepseek" and spec.has_key()), None)
    if configured is None:
        configured = ModelSpec(
            id=OCR_MODEL, model=OCR_MODEL, vendor="deepseek",
            base_url="https://api.deepseek.com/v1", api_key_env=("DEEPSEEK_API_KEY",),
        )
    return replace(configured, id=OCR_MODEL, model=OCR_MODEL, timeout=180)


def recognize(path: Path) -> dict[str, Any]:
    spec = ocr_spec()
    if not spec.has_key():
        raise providers.ProviderError("no_key", "DeepSeek OCR is not configured")
    with path.open("rb") as source:
        data = providers._post_multipart(
            spec, spec.base_url.rstrip("/") + "/tools/ocr",
            {"Authorization": f"Bearer {spec.api_key()}"},
            {"purpose": OCR_PURPOSE},
            {"file": (path.name, source, "application/pdf")},
            float(spec.timeout or 180),
        )
    if data.get("error"):
        raise providers.ProviderError("server", "DeepSeek OCR returned an error")
    try:
        pages = data["pages"]
        page_count = data["page_count"]
        if not isinstance(pages, list) or page_count != len(pages):
            raise ValueError("OCR page count mismatch")
        indices = []
        for page in pages:
            if not isinstance(page, dict) or not isinstance(page.get("text"), str):
                raise ValueError("invalid OCR page")
            index = page["index"]
            if type(index) is not int or index < 0:
                raise ValueError("invalid OCR page index")
            if page.get("finish_reason") not in ("stop", "length"):
                raise ValueError("invalid OCR finish reason")
            indices.append(index)
        if len(set(indices)) != len(indices):
            raise ValueError("duplicate OCR page index")
    except (KeyError, TypeError, ValueError) as exc:
        raise providers.ProviderError("server", "invalid DeepSeek OCR response") from exc
    return data


def parse_page(raw: str, page: Any) -> tuple[str, list[dict[str, Any]]]:
    matches = list(_MARKER.finditer(raw))
    lines: list[dict[str, Any]] = []

    def append_segment(segment: str, box: list[float] | None) -> None:
        for value in _TAGS.sub("", segment).splitlines():
            text = value.strip()
            if text:
                lines.append({"text": text, "box": box} if box else {"text": text})

    if not matches:
        append_segment(raw, None)
    else:
        append_segment(raw[:matches[0].start()], None)
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(raw)
            normalized = [float(match.group(number)) for number in range(1, 5)]
            box = [
                page.rect.x0 + normalized[0] * page.rect.width / 1000,
                page.rect.y0 + normalized[1] * page.rect.height / 1000,
                page.rect.x0 + normalized[2] * page.rect.width / 1000,
                page.rect.y0 + normalized[3] * page.rect.height / 1000,
            ]
            append_segment(raw[match.end():end], box)
    return "\n".join(item["text"] for item in lines).strip(), lines
