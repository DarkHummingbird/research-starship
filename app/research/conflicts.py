from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import select

from app.data_access.research import memory_session, migration_session
from app.research import store


COMPARISON_FIELDS = ("dataset", "split", "metric", "unit", "settings", "model_version")
RESOLUTIONS = ("overwrite", "discard", "keep_both", "edit")


def compare_facts(existing: dict[str, Any], candidate: dict[str, Any]) -> tuple[str, list[str], list[str]]:
    left = existing.get("comparison") or {}
    right = candidate.get("comparison") or {}
    missing = sorted({field for field in COMPARISON_FIELDS if not left.get(field) or not right.get(field)})
    different = sorted(field for field in COMPARISON_FIELDS if field not in missing and left[field] != right[field])
    if different:
        return "parallel", missing, different
    if missing:
        return "comparability_unknown", missing, []
    if existing.get("value") == candidate.get("value"):
        return "consistent", [], []
    return "conflict", [], []


def _public(row) -> dict[str, Any]:
    item = store.model_dict(row)
    item["existing_fact"] = store.json_value(item.pop("existing_fact_json"), {})
    item["candidate_fact"] = store.json_value(item.pop("candidate_fact_json"), {})
    item.pop("user_id", None)
    item["options"] = list(RESOLUTIONS)
    return item


def get_conflict(user_id: str, conflict_id: str):
    from app.research.service import ResearchError

    with migration_session() as conn:
        row = conn.execute(select(store.ResearchConflict).where(
            store.ResearchConflict.id == conflict_id, store.ResearchConflict.user_id == user_id
        )).scalar_one_or_none()
        if row is None:
            raise ResearchError(404, "conflict_not_found", "冲突记录不存在")
        return row


def create_conflict(
    conn,
    user_id: str,
    project_id: str,
    *,
    candidate_document_id: str,
    existing_memory_id: str,
    existing_fact: dict[str, Any],
    candidate_fact: dict[str, Any],
    comparability: str,
) -> dict[str, Any]:
    existing = conn.scalars(select(store.ResearchConflict).where(
        store.ResearchConflict.user_id == user_id,
        store.ResearchConflict.candidate_document_id == candidate_document_id,
        store.ResearchConflict.existing_memory_id == existing_memory_id,
    )).first()
    if existing is not None:
        return _public(existing)
    row = store.ResearchConflict(
        id="conf_" + uuid.uuid4().hex, user_id=user_id, project_id=project_id,
        candidate_document_id=candidate_document_id, existing_memory_id=existing_memory_id,
        existing_fact_json=json.dumps(existing_fact, ensure_ascii=False),
        candidate_fact_json=json.dumps(candidate_fact, ensure_ascii=False),
        comparability=comparability, status="pending", resolution="", created_at=store.utc_now(), resolved_at="",
    )
    conn.add(row)
    conn.flush()
    return _public(row)


def check_document_conflicts(conn, user_id: str, document) -> list[dict[str, Any]]:
    from app.research.paper_analysis import FIELDS

    if document.document_type != "paper":
        return []
    analysis = store.json_value(document.analysis_json, {})
    if not analysis:
        return []
    candidates = [fact for field in FIELDS for fact in analysis.get(field, []) if fact.get("value") is not None]
    if not candidates:
        return []
    existing_rows = conn.scalars(select(store.ResearchMemory).where(
        store.ResearchMemory.user_id == user_id,
        store.ResearchMemory.project_id == document.project_id,
        store.ResearchMemory.document_id != document.id,
        store.ResearchMemory.active.is_(True),
        store.ResearchMemory.entry_id.like("metrics-%"),
    )).all()
    conflicts = []
    warnings = []
    for candidate in candidates:
        for memory in existing_rows:
            existing = store.json_value(memory.fact_json, {})
            if not existing:
                continue
            comparability, missing, different = compare_facts(existing, candidate)
            if comparability == "conflict":
                conflicts.append(create_conflict(
                    conn, user_id, document.project_id, candidate_document_id=document.id,
                    existing_memory_id=memory.id, existing_fact=existing,
                    candidate_fact=candidate, comparability=comparability,
                ))
            elif comparability == "comparability_unknown":
                warnings.append({
                    "candidate_statement": candidate.get("statement"), "existing_statement": existing.get("statement"),
                    "missing_conditions": missing, "message": "可比性待核实",
                })
    for conflict in conflicts:
        if conflict["status"] == "pending":
            from app.research.service import ResearchError

            raise ResearchError(
                409, "metric_conflict_requires_resolution", "同条件指标冲突，已暂停写入，请选择一个处理选项",
                conflict=conflict, conflicts=conflicts, warnings=warnings,
            )
    return warnings


def apply_conflict_resolutions(conn, user_id: str, document_id: str) -> None:
    conflicts = conn.scalars(select(store.ResearchConflict).where(
        store.ResearchConflict.user_id == user_id,
        store.ResearchConflict.candidate_document_id == document_id,
        store.ResearchConflict.status == "resolved",
    )).all()
    for conflict in conflicts:
        if conflict.resolution == "overwrite":
            memory = conn.scalar(select(store.ResearchMemory).where(
                store.ResearchMemory.id == conflict.existing_memory_id,
                store.ResearchMemory.user_id == user_id,
            ))
            if memory is not None:
                memory.active = False
                memory.review_status = "superseded"


def resolve_conflict(
    user_id: str,
    conflict_id: str,
    *,
    resolution: str,
    edited_fact: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from app.research.service import ResearchError, _document
    from app.research.paper_analysis import validate_analysis

    if resolution not in RESOLUTIONS:
        raise ResearchError(400, "invalid_conflict_resolution", "冲突处理选项无效")
    with memory_session() as conn:
        row = conn.execute(select(store.ResearchConflict).where(
            store.ResearchConflict.id == conflict_id, store.ResearchConflict.user_id == user_id
        )).scalar_one_or_none()
        if row is None:
            raise ResearchError(404, "conflict_not_found", "冲突记录不存在")
        if row.status == "resolved":
            if row.resolution == resolution:
                result = _public(row)
                result["idempotent"] = True
                return result
            raise ResearchError(409, "conflict_already_resolved", "冲突已处理，不能重复覆盖决定")
        document = _document(conn, user_id, row.candidate_document_id)
        if resolution == "edit":
            if not isinstance(edited_fact, dict):
                raise ResearchError(422, "edited_fact_required", "人工修正需要提供编辑后的事实")
            analysis = store.json_value(document.analysis_json, {})
            candidate = store.json_value(row.candidate_fact_json, {})
            matched = False
            for field in ("method", "baseline", "datasets", "metrics", "limitations"):
                for index, fact in enumerate(analysis.get(field, [])):
                    if fact.get("statement") == candidate.get("statement"):
                        analysis[field][index] = edited_fact
                        matched = True
            if not matched:
                raise ResearchError(409, "candidate_fact_changed", "候选事实已变化，请重新预览冲突")
            chunks = [store.model_dict(chunk) for chunk in conn.execute(select(store.ResearchChunk).where(
                store.ResearchChunk.document_id == document.id,
                store.ResearchChunk.document_version == document.version,
            )).scalars().all()]
            try:
                validate_analysis(analysis, chunks)
            except ValueError as exc:
                raise ResearchError(422, "invalid_extraction", f"人工修正未通过校验：{exc}") from exc
            document.analysis_json = json.dumps(analysis, ensure_ascii=False)
            row.candidate_fact_json = json.dumps(edited_fact, ensure_ascii=False)
        if resolution == "discard":
            document.status = "rejected"
            document.approved_version = None
            document.updated_at = store.utc_now()
        now = store.utc_now()
        row.status = "resolved"
        row.resolution = resolution
        row.resolved_at = now
        related = conn.execute(select(store.ResearchConflict).where(
            store.ResearchConflict.user_id == user_id,
            store.ResearchConflict.candidate_document_id == row.candidate_document_id,
            store.ResearchConflict.status == "pending",
        )).scalars().all()
        for item in related:
            item.status = "resolved"
            item.resolution = resolution
            item.resolved_at = now
        conn.flush()
        result = _public(row)
        result["idempotent"] = False
        result["next_action"] = "review_candidate" if resolution in ("overwrite", "keep_both", "edit") else "none"
        return result


def list_conflicts(user_id: str, project_id: str) -> list[dict[str, Any]]:
    with migration_session() as conn:
        rows = conn.execute(select(store.ResearchConflict).where(
            store.ResearchConflict.user_id == user_id, store.ResearchConflict.project_id == project_id
        ).order_by(store.ResearchConflict.created_at.desc())).scalars().all()
        return [_public(row) for row in rows]
