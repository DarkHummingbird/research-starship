from __future__ import annotations

import json
from typing import Any

from app.data_access.research import (
    deactivate_note_memories,
    note_source_chunk,
    note_source_chunks,
    note_source_document,
    note_source_document_by_id,
    notes_dependent_on_source,
)
from app.research import gateway, store


NOTE_TYPES = {"source_fact", "ai_summary", "experiment_idea", "user_record"}


def validate_sources(
    conn,
    user_id: str,
    project_id: str,
    source_refs: list[dict[str, Any]],
    *,
    refresh: bool = False,
) -> list[dict[str, Any]]:
    from app.research.service import ResearchError

    normalized = []
    seen = set()
    for ref in source_refs:
        document_id = str(ref.get("document_id") or "")
        chunk_id = str(ref.get("chunk_id") or "")
        quote = str(ref.get("quote") or "")
        if not document_id or not chunk_id or not quote:
            raise ResearchError(400, "note_source_invalid", "笔记来源缺少资料、片段或逐字引文")
        source = note_source_document(conn, user_id, document_id)
        if source is None or source.project_id != project_id:
            raise ResearchError(400, "note_source_invalid", "笔记来源不属于当前用户和课题")
        if source.status != "approved" or source.approved_version != source.version:
            raise ResearchError(409, "note_source_unavailable", "笔记来源当前不是已确认的有效版本")
        version = int(ref.get("version") or 0)
        if refresh and version != source.version:
            matches = [chunk for chunk in note_source_chunks(conn, source.id, source.version) if quote in chunk.content]
            if len(matches) != 1:
                raise ResearchError(409, "note_source_changed", "来源变化后无法唯一定位原引文，请新建笔记并重新选择来源")
            chunk = matches[0]
            version = source.version
            chunk_id = chunk.id
        else:
            if version != source.version:
                raise ResearchError(409, "note_source_changed", "笔记来源版本已变化，请重新核对")
            chunk = note_source_chunk(conn, chunk_id, source.id, version)
            if chunk is None or quote not in chunk.content:
                raise ResearchError(409, "note_source_changed", "逐字引文不在当前来源片段中")
        key = (document_id, version, chunk_id)
        if key in seen:
            continue
        seen.add(key)
        normalized.append({
            "document_id": source.id,
            "version": version,
            "chunk_id": chunk_id,
            "title": source.title,
            "chapter": chunk.chapter,
            "quote": quote,
            "locator": dict(ref.get("locator") or store.json_value(chunk.locator_json, {})),
            "confidentiality": source.confidentiality,
        })
    return normalized


def refresh_for_review(conn, user_id: str, document) -> list[dict[str, Any]]:
    parsed = store.json_value(document.parsed_json, {})
    refs = validate_sources(conn, user_id, document.project_id, parsed.get("note_provenance") or [], refresh=True)
    parsed["note_provenance"] = refs
    parsed["note_validity"] = "valid"
    document.parsed_json = json.dumps(parsed, ensure_ascii=False)
    document.processing_error = ""
    return refs


def mark_source_invalid(conn, source_document_id: str, *, reason: str) -> int:
    source = note_source_document_by_id(conn, source_document_id)
    if source is None:
        return 0
    count = 0
    for note in notes_dependent_on_source(conn, source):
        parsed = store.json_value(note.parsed_json, {})
        note.status = "pending"
        note.approved_version = None
        note.processing_error = reason
        note.updated_at = store.utc_now()
        parsed["note_validity"] = "source_changed" if source.status != "deleted" else "source_withdrawn"
        note.parsed_json = json.dumps(parsed, ensure_ascii=False)
        deactivate_note_memories(conn, note.id)
        count += 1
    return count


def note_confidentiality(value: str, refs: list[dict[str, Any]]) -> str:
    return gateway.highest_level([value, *[ref["confidentiality"] for ref in refs]])
