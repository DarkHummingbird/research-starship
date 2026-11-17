from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import select

from app.data_access.research import memory_session, migration_session
from app.research import store

ENTRY_KINDS = {"open_question", "evidence_gap", "todo", "term"}
ENTRY_STATUSES = {"active", "resolved", "pending_review", "archived"}


def _id(prefix: str) -> str:
    return prefix + uuid.uuid4().hex


def public_entry(row) -> dict[str, Any]:
    item = store.model_dict(row)
    item["source_refs"] = store.json_value(item.pop("source_refs_json"), [])
    item["related"] = store.json_value(item.pop("related_json"), {})
    item.pop("user_id", None)
    return item


def _project(conn, user_id: str, project_id: str):
    from app.research.service import _project as owned_project

    return owned_project(conn, user_id, project_id)


def _entry(conn, user_id: str, entry_id: str):
    from app.research.service import ResearchError

    row = conn.scalars(select(store.ResearchStateEntry).where(
        store.ResearchStateEntry.id == entry_id, store.ResearchStateEntry.user_id == user_id,
    )).one_or_none()
    if row is None:
        raise ResearchError(404, "state_entry_not_found", "课题状态条目不存在")
    return row


def _validated_refs(conn, user_id: str, project_id: str, source_refs: list[dict[str, Any]], *, refresh: bool = False):
    from app.research.notes import validate_sources

    return validate_sources(conn, user_id, project_id, source_refs, refresh=refresh) if source_refs else []


def create_entry(
    user_id: str,
    project_id: str,
    *,
    kind: str,
    content: str,
    definition: str = "",
    abbreviation: str = "",
    source_refs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    from app.research.service import ResearchError

    if kind not in ENTRY_KINDS:
        raise ResearchError(400, "invalid_state_entry_kind", "不支持的课题状态类型")
    content = content.strip()
    definition = definition.strip()
    abbreviation = abbreviation.strip()
    if not content or len(content) > 2000 or len(definition) > 4000 or len(abbreviation) > 200:
        raise ResearchError(400, "invalid_state_entry", "内容须为 1–2000 字符，定义不超过 4000 字符，缩写不超过 200 字符")
    if abbreviation and kind != "term":
        raise ResearchError(400, "invalid_state_entry_abbreviation", "只有术语可以填写缩写")
    with memory_session() as conn:
        _project(conn, user_id, project_id)
        refs = _validated_refs(conn, user_id, project_id, list(source_refs or []))
        now = store.utc_now()
        row = store.ResearchStateEntry(
            id=_id("rse_"), user_id=user_id, project_id=project_id, kind=kind,
            content=content, definition=definition, abbreviation=abbreviation, status="active",
            source_refs_json=json.dumps(refs, ensure_ascii=False), related_json="{}",
            version=1, created_at=now, updated_at=now,
        )
        conn.add(row)
        conn.flush()
        return public_entry(row)


def update_entry(
    user_id: str,
    entry_id: str,
    *,
    expected_version: int,
    content: str | None = None,
    definition: str | None = None,
    abbreviation: str | None = None,
    status: str | None = None,
    source_refs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    from app.research.service import ResearchError

    with memory_session() as conn:
        row = _entry(conn, user_id, entry_id)
        if row.version != expected_version:
            raise ResearchError(409, "state_entry_version_changed", "条目已变化，请刷新后再保存")
        if status is not None and status not in ENTRY_STATUSES:
            raise ResearchError(400, "invalid_state_entry_status", "不支持的条目状态")
        if content is not None:
            content = content.strip()
            if not content or len(content) > 2000:
                raise ResearchError(400, "invalid_state_entry", "内容须为 1–2000 字符")
            row.content = content
        if definition is not None:
            definition = definition.strip()
            if len(definition) > 4000:
                raise ResearchError(400, "invalid_state_entry", "定义不能超过 4000 字符")
            row.definition = definition
        if abbreviation is not None:
            abbreviation = abbreviation.strip()
            if len(abbreviation) > 200 or abbreviation and row.kind != "term":
                raise ResearchError(400, "invalid_state_entry_abbreviation", "术语缩写不能超过 200 字符，且只能用于术语")
            row.abbreviation = abbreviation
        refs = list(source_refs) if source_refs is not None else store.json_value(row.source_refs_json, [])
        if status != "archived" and (refs or row.status == "pending_review" and status in {"active", "resolved"}):
            refs = _validated_refs(conn, user_id, row.project_id, refs, refresh=True)
            row.source_refs_json = json.dumps(refs, ensure_ascii=False)
        if status is not None:
            row.status = status
        row.version += 1
        row.updated_at = store.utc_now()
        conn.flush()
        return public_entry(row)


def mark_entries_invalid(conn, source_document_id: str, *, reason: str) -> int:
    source = conn.get(store.ResearchDocument, source_document_id)
    if source is None:
        return 0
    rows = conn.scalars(select(store.ResearchStateEntry).where(
        store.ResearchStateEntry.user_id == source.user_id,
        store.ResearchStateEntry.project_id == source.project_id,
        store.ResearchStateEntry.status != "archived",
    )).all()
    count = 0
    for row in rows:
        refs = store.json_value(row.source_refs_json, [])
        if not any(ref.get("document_id") == source_document_id for ref in refs):
            continue
        row.status = "pending_review"
        row.definition = reason
        row.version += 1
        row.updated_at = store.utc_now()
        count += 1
    return count


def create_related_notices(conn, user_id: str, document_id: str) -> int:
    from app.research.service import _terms

    source = conn.get(store.ResearchDocument, document_id)
    if source is None or source.status != "approved" or source.document_type == "research_note":
        return 0
    source_terms = _terms(source.title + " " + source.source_text[:4000])
    notes = conn.scalars(select(store.ResearchDocument).where(
        store.ResearchDocument.user_id == user_id,
        store.ResearchDocument.project_id == source.project_id,
        store.ResearchDocument.document_type == "research_note",
        store.ResearchDocument.status == "approved",
    )).all()
    existing = conn.scalars(select(store.ResearchStateEntry).where(
        store.ResearchStateEntry.user_id == user_id,
        store.ResearchStateEntry.project_id == source.project_id,
        store.ResearchStateEntry.kind == "review_notice",
        store.ResearchStateEntry.status != "archived",
    )).all()
    existing_notes = {store.json_value(row.related_json, {}).get("note_id") for row in existing
                      if store.json_value(row.related_json, {}).get("new_document_id") == source.id}
    chunk = conn.scalars(select(store.ResearchChunk).where(
        store.ResearchChunk.document_id == source.id,
        store.ResearchChunk.document_version == source.version,
    ).order_by(store.ResearchChunk.ordinal)).first()
    if chunk is None:
        return 0
    count = 0
    for note in notes:
        if note.id in existing_notes:
            continue
        note_terms = _terms(note.title + " " + note.source_text[:3000])
        shared = source_terms & note_terms
        title_shared = _terms(note.title) & _terms(source.title)
        if len(shared) < 3 or not title_shared:
            continue
        now = store.utc_now()
        refs = [{
            "document_id": source.id, "version": source.version, "chunk_id": chunk.id,
            "title": source.title, "chapter": chunk.chapter, "quote": chunk.content[:400],
            "locator": store.json_value(chunk.locator_json, {}), "confidentiality": source.confidentiality,
        }]
        row = store.ResearchStateEntry(
            id=_id("rse_"), user_id=user_id, project_id=source.project_id, kind="review_notice",
            content=f"新资料《{source.title}》与既有笔记《{note.title}》相关，请核对是否存在补充或冲突",
            definition="仅提示相关内容；没有自动判定冲突，也没有使原笔记失效",
            status="pending_review", source_refs_json=json.dumps(refs, ensure_ascii=False),
            related_json=json.dumps({"note_id": note.id, "new_document_id": source.id}, ensure_ascii=False),
            version=1, created_at=now, updated_at=now,
        )
        conn.add(row)
        count += 1
        if count >= 5:
            break
    return count


def project_state(user_id: str, project_id: str) -> dict[str, Any]:
    from app.research.experiments import public_experiment

    with migration_session() as conn:
        project = _project(conn, user_id, project_id)
        entries = conn.scalars(select(store.ResearchStateEntry).where(
            store.ResearchStateEntry.user_id == user_id,
            store.ResearchStateEntry.project_id == project_id,
            store.ResearchStateEntry.status != "archived",
        ).order_by(store.ResearchStateEntry.updated_at.desc())).all()
        notes = conn.scalars(select(store.ResearchDocument).where(
            store.ResearchDocument.user_id == user_id,
            store.ResearchDocument.project_id == project_id,
            store.ResearchDocument.document_type == "research_note",
            store.ResearchDocument.status == "approved",
        ).order_by(store.ResearchDocument.updated_at.desc())).all()
        experiments = conn.scalars(select(store.ResearchExperiment).where(
            store.ResearchExperiment.user_id == user_id,
            store.ResearchExperiment.project_id == project_id,
        ).order_by(store.ResearchExperiment.updated_at.desc())).all()
        baseline = conn.scalars(select(store.ResearchDocument).where(
            store.ResearchDocument.id == project.current_baseline_id,
            store.ResearchDocument.user_id == user_id,
            store.ResearchDocument.project_id == project_id,
        )).one_or_none() if project.current_baseline_id else None
        baseline_data = None
        if baseline:
            parsed = store.json_value(baseline.parsed_json, {})
            validity = parsed.get("note_validity", "valid")
            review_required = (
                baseline.status != "approved" or baseline.approved_version != baseline.version
                or validity in {"source_changed", "source_withdrawn"}
            )
            baseline_data = {
                "id": baseline.id, "title": baseline.title, "version": baseline.version,
                "status": baseline.status, "approved_version": baseline.approved_version,
                "validity": validity, "review_required": review_required,
                "review_reason": baseline.processing_error if review_required else "",
            }
        grouped = {kind: [public_entry(row) for row in entries if row.kind == kind] for kind in (*ENTRY_KINDS, "review_notice")}
        pending_experiments = [public_experiment(row) for row in experiments if row.status == "pending_review"]
        return {
            "project": {
                "id": project.id, "name": project.name, "goal": project.goal,
                "research_question": project.research_question, "next_action": project.next_action,
                "updated_at": project.updated_at,
            },
            "baseline": baseline_data,
            **grouped,
            "confirmed_conclusions": [{
                "id": note.id, "title": note.title, "content": note.source_text,
                "version": note.version, "note_type": store.json_value(note.parsed_json, {}).get("note_type", "user_record"),
            } for note in notes[:20]],
            "experiments": [public_experiment(row) for row in experiments],
            "experiment_summary": {
                status: sum(row.status == status for row in experiments)
                for status in ("idea", "in_progress", "succeeded", "failed", "pending_review")
            },
            "pending_review": [public_entry(row) for row in entries if row.status == "pending_review"],
            "pending_experiments": pending_experiments,
            "counts": {
                "confirmed_conclusions": len(notes), "open_questions": len(grouped["open_question"]),
                "evidence_gaps": len(grouped["evidence_gap"]), "terms": len(grouped["term"]),
                "experiments": len(experiments),
                "pending_review": sum(row.status == "pending_review" for row in entries) + len(pending_experiments),
            },
        }


def update_project_state(
    user_id: str,
    project_id: str,
    *,
    research_question: str | None = None,
    next_action: str | None = None,
    expected_updated_at: str,
) -> dict[str, Any]:
    from app.research.service import ResearchError

    with memory_session() as conn:
        project = _project(conn, user_id, project_id)
        if project.updated_at != expected_updated_at:
            raise ResearchError(409, "project_state_changed", "课题状态已变化，请刷新后再保存")
        if research_question is not None:
            value = research_question.strip()
            if len(value) > 2000:
                raise ResearchError(400, "invalid_research_question", "核心问题不能超过 2000 字符")
            project.research_question = value
        if next_action is not None:
            value = next_action.strip()
            if len(value) > 2000:
                raise ResearchError(400, "invalid_next_action", "下一步建议不能超过 2000 字符")
            project.next_action = value
        project.updated_at = store.utc_now()
        conn.flush()
    return project_state(user_id, project_id)
