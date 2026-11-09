from __future__ import annotations

import re
from typing import Any


STRATEGIES = {"auto", "parent_recursive", "recursive", "fixed", "semantic"}


def split_fixed(text: str, *, max_chars: int = 5000, overlap: int = 200) -> list[str]:
    text = text.strip()
    if len(text) <= max_chars:
        return [text] if text else []
    parts = []
    start = 0
    while start < len(text):
        end = min(len(text), start + max_chars)
        if end < len(text):
            boundary = text.rfind("\n", start + max_chars // 2, end)
            if boundary > start:
                end = boundary
        parts.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(start + 1, end - overlap)
    return [part for part in parts if part]


def _pack_units(units: list[str], separator: str, max_chars: int, overlap: int) -> list[str]:
    output = []
    current = ""
    for index, unit in enumerate(units):
        if index < len(units) - 1:
            unit += separator
        if len(unit) > max_chars:
            if current.strip():
                output.append(current.strip())
                current = ""
            output.extend(split_fixed(unit, max_chars=max_chars, overlap=overlap))
        elif len(current) + len(unit) <= max_chars:
            current += unit
        else:
            if current.strip():
                output.append(current.strip())
            current = unit
    if current.strip():
        output.append(current.strip())
    return output


def split_recursive(text: str, *, max_chars: int = 1600, overlap: int = 200) -> list[str]:
    text = text.strip()
    if len(text) <= max_chars:
        return [text] if text else []
    for separator in ("\n\n", "\n", "。", "！", "？", ". ", "! ", "? ", " "):
        if separator not in text:
            continue
        units = text.split(separator)
        if len(units) > 1:
            return _pack_units(units, separator, max_chars, overlap)
    return split_fixed(text, max_chars=max_chars, overlap=overlap)


def split_semantic(text: str, *, max_chars: int = 1600, threshold: float = 0.45) -> list[str]:
    from app.research.local_components import LocalComponentError, embed_texts

    units = [
        unit.strip() for unit in re.split(r"(?<=[。！？.!?])\s+|\n\s*\n", text)
        if unit.strip()
    ]
    if len(units) <= 1:
        return [text.strip()] if text.strip() else []
    try:
        vectors = embed_texts(units)["vectors"]
    except LocalComponentError as exc:
        raise ValueError("语义切块需要本地 BGE-M3；组件不可用时不会改用在线服务") from exc
    if not isinstance(vectors, list) or len(vectors) != len(units):
        raise ValueError("本地语义向量数量与切块单元不一致")
    chunks = []
    current = units[0]
    for index, unit in enumerate(units[1:], 1):
        similarity = sum(float(a) * float(b) for a, b in zip(vectors[index - 1], vectors[index], strict=False))
        if len(current) >= 300 and similarity < threshold:
            chunks.append(current)
            current = unit
        elif len(current) + len(unit) + 1 > max_chars:
            chunks.append(current)
            current = unit
        else:
            current += "\n" + unit
    if current:
        chunks.append(current)
    return chunks


def chunk_text(text: str, strategy: str = "auto") -> list[str]:
    if strategy not in STRATEGIES:
        raise ValueError("切块方式支持自动、父子递归、递归、固定长度和本地语义")
    if strategy == "fixed":
        return split_fixed(text)
    if strategy == "semantic":
        return split_semantic(text)
    return split_recursive(text)


def parent_metadata(
    *,
    parent_id: str,
    parent_chapter: str,
    parent_content: str,
    strategy: str,
    locator: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        **(locator or {}),
        "parent_id": parent_id,
        "parent_chapter": parent_chapter,
        "parent_content": parent_content if strategy in ("auto", "parent_recursive", "semantic") else "",
        "chunking_strategy": strategy,
    }
