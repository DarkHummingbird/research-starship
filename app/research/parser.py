from __future__ import annotations

import re
from typing import Any

from jsonschema import Draft202012Validator

from app.research.chunking import chunk_text, parent_metadata


DOCUMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["schema_version", "title", "record_type", "research_fields", "entries"],
    "properties": {
        "schema_version": {"const": "research-document/v1"},
        "title": {"type": "string", "minLength": 1, "maxLength": 300},
        "record_type": {"const": "source_statement"},
        "note_type": {"enum": ["source_fact", "ai_summary", "experiment_idea", "user_record"]},
        "note_validity": {"enum": ["valid", "source_changed", "source_withdrawn"]},
        "note_provenance": {
            "type": "array",
            "maxItems": 20,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["document_id", "version", "chunk_id", "title", "chapter", "quote", "locator", "confidentiality"],
                "properties": {
                    "document_id": {"type": "string", "minLength": 1},
                    "version": {"type": "integer", "minimum": 1},
                    "chunk_id": {"type": "string", "minLength": 1},
                    "title": {"type": "string"},
                    "chapter": {"type": "string"},
                    "quote": {"type": "string", "minLength": 1},
                    "locator": {"type": "object"},
                    "confidentiality": {"enum": ["L1", "L2", "L3"]},
                },
            },
        },
        "model_info": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"model": {"type": "string"}, "task_id": {"type": "string"}},
        },
        "research_fields": {
            "type": "object",
            "additionalProperties": False,
            "required": ["method", "baseline", "datasets", "metrics", "limitations"],
            "properties": {
                "method": {"type": "null"},
                "baseline": {"type": "null"},
                "datasets": {"type": "array", "items": {"type": "string"}, "maxItems": 0},
                "metrics": {"type": "array", "items": {"type": "string"}, "maxItems": 0},
                "limitations": {"type": "array", "items": {"type": "string"}, "maxItems": 0},
            },
        },
        "entries": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["entry_id", "statement", "confidentiality", "review_status", "suspicious", "evidence"],
                "properties": {
                    "entry_id": {"type": "string", "pattern": "^entry-[0-9]+$"},
                    "statement": {"type": "string", "minLength": 1},
                    "confidentiality": {"enum": ["L1", "L2", "L3"]},
                    "review_status": {"const": "pending"},
                    "suspicious": {"type": "array", "items": {"type": "string"}},
                    "evidence": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["document_id", "version", "chunk_id", "chapter", "quote"],
                        "properties": {
                            "document_id": {"type": "string", "minLength": 1},
                            "version": {"type": "integer", "minimum": 1},
                            "chunk_id": {"type": "string", "minLength": 1},
                            "chapter": {"type": "string", "minLength": 1},
                            "quote": {"type": "string", "minLength": 1},
                        },
                    },
                },
            },
        },
    },
}


class ExtractionValidationError(ValueError):
    def __init__(self, issues: list[str]):
        super().__init__("; ".join(issues))
        self.issues = issues


def _issues(payload: Any) -> list[str]:
    validator = Draft202012Validator(DOCUMENT_SCHEMA)
    return [f"{'/'.join(map(str, error.path)) or '$'}: {error.message}" for error in validator.iter_errors(payload)]


def validate_document_card(payload: Any, chunks: list[dict[str, Any]]) -> None:
    issues = _issues(payload)
    if issues:
        raise ExtractionValidationError(issues)
    by_id = {chunk["id"]: chunk for chunk in chunks}
    for index, entry in enumerate(payload["entries"]):
        evidence = entry["evidence"]
        chunk = by_id.get(evidence["chunk_id"])
        if chunk is None:
            issues.append(f"entries/{index}/evidence/chunk_id: 来源 ID 不存在")
            continue
        if evidence["document_id"] != chunk["document_id"] or evidence["version"] != chunk["document_version"]:
            issues.append(f"entries/{index}/evidence: 资料 ID 或版本不匹配")
        if evidence["chapter"] != chunk["chapter"]:
            issues.append(f"entries/{index}/evidence/chapter: 章节不匹配")
        if evidence["quote"] not in chunk["content"]:
            issues.append(f"entries/{index}/evidence/quote: 原文片段不可核验")
    if issues:
        raise ExtractionValidationError(issues)


def validate_p0_model_result(data: Any, valid_chapters: set[int]) -> None:
    if not isinstance(data, dict):
        raise ExtractionValidationError(["$: 必须是对象"])
    issues: list[str] = []
    rules = {
        "entry": (dict, "入库规则"),
        "retention_days": (int, "回收站天数"),
        "confidential": (dict, "保密规则"),
    }
    for key, (expected, label) in rules.items():
        value = data.get(key)
        if expected is int:
            if type(value) is not int or not 0 < value <= 3650:
                issues.append(f"{key}: {label}必须是 1–3650 的整数")
            continue
        if not isinstance(value, expected):
            issues.append(f"{key}: {label}必须是对象")
            continue
        if not isinstance(value.get("rule"), str) or not value["rule"].strip():
            issues.append(f"{key}/rule: 必须是非空字符串")
        source = value.get("source")
        chapter = source.get("chapter") if isinstance(source, dict) else None
        if type(chapter) is not int or chapter not in valid_chapters:
            issues.append(f"{key}/source/chapter: 来源章节不存在")
    unknown = set(data) - set(rules)
    if unknown:
        issues.append(f"$: 未声明字段 {sorted(unknown)}")
    if issues:
        raise ExtractionValidationError(issues)


def _suspicious(text: str) -> list[str]:
    flags: list[str] = []
    if "\ufffd" in text:
        flags.append("存在乱码替换符，请复核解析或 OCR")
    if re.search(r"(?:TODO|待补|待核验|无法识别)", text, re.I):
        flags.append("原文含待补或待核验标记")
    if len(text) > 20000:
        flags.append("章节过长，召回时可能需要人工拆分")
    return flags


def parse_markdown(
    text: str,
    *,
    document_id: str,
    version: int,
    title: str,
    confidentiality: str,
    chunking_strategy: str = "auto",
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    heading_title = next((line[2:].strip() for line in lines if line.startswith("# ")), "")
    sections: list[tuple[str, list[str]]] = []
    chapter = "文档说明"
    body: list[str] = []
    for line in lines:
        if line.startswith("## "):
            if "\n".join(body).strip():
                sections.append((chapter, body))
            chapter = line[3:].strip() or "未命名章节"
            body = []
        else:
            body.append(line)
    if "\n".join(body).strip():
        sections.append((chapter, body))
    sections = [(heading, content) for heading, content in sections if "\n".join(content).strip()]
    if not sections:
        raise ValueError("Markdown 没有可解析的正文")

    chunks: list[dict[str, Any]] = []
    entries: list[dict[str, Any]] = []
    for section_ordinal, (section_title, content_lines) in enumerate(sections, 1):
        content = "\n".join(content_lines).strip()
        parts = chunk_text(content, chunking_strategy)
        for part_index, part in enumerate(parts, 1):
            ordinal = len(chunks) + 1
            chapter = section_title + (f"（片段 {part_index}）" if len(parts) > 1 else "")
            chunk_id = f"{document_id}:v{version}:c{ordinal}"
            chunks.append({
                "id": chunk_id, "document_id": document_id, "document_version": version,
                "ordinal": ordinal, "chapter": chapter, "content": part,
                "confidentiality": confidentiality,
                "locator_json": parent_metadata(
                    parent_id=f"{document_id}:v{version}:p{section_ordinal}",
                    parent_chapter=section_title, parent_content=content,
                    strategy=chunking_strategy, locator={"chapter": section_title, "extraction": "markdown"},
                ),
            })
            entries.append({
                "entry_id": f"entry-{ordinal}", "statement": part,
                "confidentiality": confidentiality, "review_status": "pending",
                "suspicious": _suspicious(part),
                "evidence": {
                    "document_id": document_id, "version": version,
                    "chunk_id": chunk_id, "chapter": chapter, "quote": part,
                },
            })
    card = {
        "schema_version": "research-document/v1",
        "title": heading_title or title,
        "record_type": "source_statement",
        "research_fields": {"method": None, "baseline": None, "datasets": [], "metrics": [], "limitations": []},
        "entries": entries,
    }
    validate_document_card(card, chunks)
    return card, chunks
