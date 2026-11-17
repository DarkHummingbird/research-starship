from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import select

from app.data_access.research import memory_session, migration_session
from app.research import store

EXPERIMENT_STATUSES = {"idea", "in_progress", "succeeded", "failed", "pending_review"}
EDITABLE_FIELDS = {
    "title", "hypothesis", "variables", "controls", "data_version", "code_version",
    "result", "failure_reason", "next_step",
}


def public_experiment(row) -> dict[str, Any]:
    item = store.model_dict(row)
    item["result_source_refs"] = store.json_value(item.pop("result_source_refs_json"), [])
    item.pop("user_id", None)
    item["experiments_executed"] = row.status in {"succeeded", "failed"} or (
        row.status == "pending_review" and row.status_before_review in {"succeeded", "failed"}
    )
    return item


def create_experiment(user_id: str, project_id: str, **fields: Any) -> dict[str, Any]:
    from app.research.service import ResearchError, _project

    title = str(fields.get("title") or "").strip()
    if not title or len(title) > 300:
        raise ResearchError(400, "invalid_experiment_title", "实验名称须为 1–300 字符")
    with memory_session() as conn:
        _project(conn, user_id, project_id)
        now = store.utc_now()
        values = {name: str(fields.get(name) or "").strip()[:8000] for name in EDITABLE_FIELDS - {"title"}}
        row = store.ResearchExperiment(
            id="exp_" + uuid.uuid4().hex, user_id=user_id, project_id=project_id,
            title=title, **values, status="idea", result_source_refs_json="[]",
            version=1, created_at=now, updated_at=now, completed_at="",
            status_before_review="", review_reason="",
        )
        conn.add(row)
        conn.flush()
        return public_experiment(row)


def list_experiments(user_id: str, project_id: str) -> list[dict[str, Any]]:
    from app.research.service import _project

    with migration_session() as conn:
        _project(conn, user_id, project_id)
        rows = conn.execute(select(store.ResearchExperiment).where(
            store.ResearchExperiment.user_id == user_id,
            store.ResearchExperiment.project_id == project_id,
        ).order_by(store.ResearchExperiment.updated_at.desc())).scalars().all()
        return [public_experiment(row) for row in rows]


def update_experiment(
    user_id: str,
    experiment_id: str,
    *,
    expected_version: int,
    fields: dict[str, Any],
    status: str | None = None,
    result_source_refs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    from app.research.service import ResearchError
    from app.research.notes import validate_sources

    with memory_session() as conn:
        row = conn.execute(select(store.ResearchExperiment).where(
            store.ResearchExperiment.id == experiment_id, store.ResearchExperiment.user_id == user_id,
        )).scalar_one_or_none()
        if row is None:
            raise ResearchError(404, "experiment_not_found", "实验卡片不存在")
        if row.version != expected_version:
            raise ResearchError(409, "experiment_version_changed", "实验卡片已变化，请刷新后再保存")
        if status is not None and status not in EXPERIMENT_STATUSES:
            raise ResearchError(400, "invalid_experiment_status", "不支持的实验状态")
        for name, value in fields.items():
            if name not in EDITABLE_FIELDS:
                raise ResearchError(400, "invalid_experiment_field", f"不支持修改字段：{name}")
            text = str(value or "").strip()
            if name == "title" and not text:
                raise ResearchError(400, "invalid_experiment_title", "实验名称不能为空")
            setattr(row, name, text[:8000])
        if result_source_refs is not None:
            refs = validate_sources(conn, user_id, row.project_id, result_source_refs, refresh=True)
            row.result_source_refs_json = json.dumps(refs, ensure_ascii=False)
        elif status is not None and status != "pending_review" and row.status == "pending_review":
            refs = store.json_value(row.result_source_refs_json, [])
            if refs:
                refs = validate_sources(conn, user_id, row.project_id, refs, refresh=True)
                row.result_source_refs_json = json.dumps(refs, ensure_ascii=False)
        if status is not None:
            if status != "pending_review" and row.status == "pending_review":
                row.review_reason = ""
                row.status_before_review = ""
            row.status = status
            if status in {"succeeded", "failed"} and not row.completed_at:
                row.completed_at = store.utc_now()
            elif status not in {"succeeded", "failed"}:
                row.completed_at = ""
        row.version += 1
        row.updated_at = store.utc_now()
        conn.flush()
        return public_experiment(row)


def mark_experiments_invalid(conn, source_document_id: str, *, reason: str) -> int:
    source = conn.get(store.ResearchDocument, source_document_id)
    if source is None:
        return 0
    rows = conn.scalars(select(store.ResearchExperiment).where(
        store.ResearchExperiment.user_id == source.user_id,
        store.ResearchExperiment.project_id == source.project_id,
    )).all()
    count = 0
    for row in rows:
        refs = store.json_value(row.result_source_refs_json, [])
        if not any(ref.get("document_id") == source_document_id for ref in refs):
            continue
        if row.status != "pending_review":
            row.status_before_review = row.status
            row.version += 1
            count += 1
        row.status = "pending_review"
        row.review_reason = reason
        row.updated_at = store.utc_now()
    return count
