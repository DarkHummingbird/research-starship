"""Locate source text without guessing when a page has ambiguous matches."""
from __future__ import annotations

import re
import unicodedata
from typing import Any


def compact(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text)).casefold()


def unique_start(text: str, key: str) -> int:
    start = text.find(key)
    return start if start >= 0 and text.find(key, start + 1) < 0 else -1


def page_rectangles(page: Any, quote: str, locator: dict[str, Any]) -> list[Any]:
    import fitz

    key = compact(quote)
    if not key:
        return []
    if locator.get("extraction") == "pdf_text":
        # Map normalized characters to their actual PDF glyph boxes. This handles
        # PDF ligatures, spacing differences, and line-end hyphens without
        # guessing a location when the same normalized passage occurs twice.
        text, positions = [], []
        for block in page.get_text("rawdict")["blocks"]:
            for line in block.get("lines", []):
                line_number = len(positions)
                for span in line.get("spans", []):
                    for char in span.get("chars", []):
                        part = compact(char.get("c", ""))
                        for symbol in part:
                            text.append(symbol)
                            positions.append((line_number, char["bbox"]))
        start = unique_start("".join(text), key)
        if start < 0:
            return []
        result = []
        previous_line = None
        for line_number, bbox in positions[start:start + len(key)]:
            rect = fitz.Rect(bbox)
            if line_number != previous_line:
                result.append(rect)
                previous_line = line_number
            else:
                result[-1] |= rect
        return result
    if locator.get("extraction") != "offline_ocr":
        return []
    lines = locator.get("ocr_lines")
    if not isinstance(lines, list):
        return []
    text = ""
    owners = []
    for index, line in enumerate(lines):
        if not isinstance(line, dict) or not isinstance(line.get("text"), str):
            return []
        part = compact(line["text"])
        text += part
        owners.extend([index] * len(part))
    start = unique_start(text, key)
    if start < 0:
        return []
    result = []
    for index in dict.fromkeys(owners[start:start + len(key)]):
        box = lines[index].get("box")
        if not isinstance(box, list) or len(box) != 4:
            return []
        try:
            rect = fitz.Rect(*box)
        except (TypeError, ValueError):
            return []
        if rect.is_empty or rect.is_infinite or not page.rect.contains(rect):
            return []
        result.append(rect * page.derotation_matrix)
    return result
