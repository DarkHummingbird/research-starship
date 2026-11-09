from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select, update

from app.data_access.research import memory_session, migration_session
from app.research import store


def document_state(row) -> dict[str, Any]:
    return store.public_document(row, include_text=True)


def snapshot(conn, row, operation: str) -> dict[str, Any]:
    version = store.ResearchDocumentVersion(
        id="ver_" + uuid.uuid4().hex, user_id=row.user_id, project_id=row.project_id,
        document_id=row.id, version=row.version, status=row.status, title=row.title,
        confidentiality=row.confidentiality, parsed_json=row.parsed_json, analysis_json=row.analysis_json,
        analysis_raw_json=row.analysis_raw_json,
        source_text=row.source_text, sha256=row.sha256, stored_path=row.stored_path,
        document_type=row.document_type, processing_status=row.processing_status,
        analysis_status=row.analysis_status, extraction_model=row.extraction_model,
        created_at=store.utc_now(), operation=operation,
    )
    conn.add(version)
    conn.flush()
    return store.model_dict(version)


def record_change(conn, row, *, operation: str, before: dict[str, Any], after: dict[str, Any], reason: str = "") -> dict[str, Any]:
    change = store.ResearchChange(
        id="chg_" + uuid.uuid4().hex, user_id=row.user_id, project_id=row.project_id,
        object_type="document", object_id=row.id, operation=operation, document_version=row.version,
        before_json=json.dumps(before, ensure_ascii=False), after_json=json.dumps(after, ensure_ascii=False),
        reason=reason[:500], created_at=store.utc_now(), undone_at="",
    )
    conn.add(change)
    conn.flush()
    return public_change(change)


def public_change(row) -> dict[str, Any]:
    item = store.model_dict(row)
    item["before"] = store.json_value(item.pop("before_json"), {})
    item["after"] = store.json_value(item.pop("after_json"), {})
    item.pop("user_id", None)
    item["undoable"] = not item["undone_at"] and item["operation"] != "permanent_delete"
    return item


def list_versions(user_id: str, document_id: str) -> list[dict[str, Any]]:
    with migration_session() as conn:
        rows = conn.execute(select(store.ResearchDocumentVersion).where(
            store.ResearchDocumentVersion.user_id == user_id,
            store.ResearchDocumentVersion.document_id == document_id,
        ).order_by(store.ResearchDocumentVersion.created_at.desc())).scalars().all()
        result = []
        for row in rows:
            item = store.model_dict(row)
            item.pop("user_id", None)
            item.pop("source_text", None)
            item.pop("stored_path", None)
            item["parsed"] = store.json_value(item.pop("parsed_json"), {})
            item["analysis"] = store.json_value(item.pop("analysis_json"), {})
            result.append(item)
        return result


def list_changes(user_id: str, project_id: str, *, limit: int = 10) -> list[dict[str, Any]]:
    with migration_session() as conn:
        rows = conn.execute(select(store.ResearchChange).where(
            store.ResearchChange.user_id == user_id, store.ResearchChange.project_id == project_id
        ).order_by(store.ResearchChange.created_at.desc()).limit(limit)).scalars().all()
        return [public_change(row) for row in rows]


def _clone_version_content(conn, document, version: int, new_version: int) -> tuple[dict[str, Any], dict[str, Any]]:
    parsed = store.json_value(document.parsed_json, {})
    analysis = store.json_value(document.analysis_json, {})
    old_chunks = conn.scalars(select(store.ResearchChunk).where(
        store.ResearchChunk.document_id == document.id,
        store.ResearchChunk.document_version == version,
    ).order_by(store.ResearchChunk.ordinal)).all()
    for chunk in old_chunks:
        conn.add(store.ResearchChunk(
            id=f"{document.id}:v{new_version}:c{chunk.ordinal}", user_id=document.user_id,
            project_id=document.project_id, document_id=document.id, document_version=new_version,
            ordinal=chunk.ordinal, chapter=chunk.chapter, content=chunk.content,
            confidentiality=document.confidentiality, locator_json=chunk.locator_json,
        ))
    for entry in parsed.get("entries", []):
        ordinal = int(entry["entry_id"].split("-")[-1])
        entry["review_status"] = "pending"
        entry["confidentiality"] = document.confidentiality
        entry["evidence"]["version"] = new_version
        entry["evidence"]["chunk_id"] = f"{document.id}:v{new_version}:c{ordinal}"
    for field in ("method", "baseline", "datasets", "metrics", "limitations"):
        for fact in analysis.get(field, []):
            old_id = fact.get("evidence", {}).get("chunk_id", "")
            match = re_match_chunk(old_id)
            if match:
                fact["evidence"]["chunk_id"] = f"{document.id}:v{new_version}:c{match}"
    return parsed, analysis


def re_match_chunk(chunk_id: str) -> int | None:
    import re

    match = re.search(r":c(\d+)$", chunk_id)
    return int(match.group(1)) if match else None


def restore_version(
    user_id: str,
    document_id: str,
    *,
    target_version: int,
    expected_version: int,
    confirmed: bool,
    operation: str = "restore",
) -> dict[str, Any]:
    from app.research.service import ResearchError, _document

    if not confirmed:
        raise ResearchError(409, "restore_confirmation_required", "恢复历史版本需要明确确认")
    with memory_session() as conn:
        row = _document(conn, user_id, document_id)
        if row.version != expected_version:
            raise ResearchError(409, "document_version_changed", "资料版本已变化，请重新预览")
        target = conn.execute(select(store.ResearchDocumentVersion).where(
            store.ResearchDocumentVersion.user_id == user_id,
            store.ResearchDocumentVersion.document_id == document_id,
            store.ResearchDocumentVersion.version == target_version,
        )).scalars().first()
        if target is None:
            raise ResearchError(404, "version_not_found", "历史版本不存在")
        before = document_state(row)
        snapshot(conn, row, "before_" + operation)
        row.version += 1
        row.title = target.title
        row.confidentiality = target.confidentiality
        row.status = "pending"
        row.approved_version = None
        row.document_type = target.document_type
        row.processing_status = target.processing_status
        row.analysis_status = target.analysis_status
        row.extraction_model = target.extraction_model
        row.source_text = target.source_text
        row.sha256 = target.sha256
        row.stored_path = target.stored_path
        row.deleted_at = ""
        row.delete_after = ""
        row.permanent_deleted_at = ""
        row.parsed_json = target.parsed_json
        row.analysis_json = target.analysis_json
        row.analysis_raw_json = target.analysis_raw_json
        parsed, analysis = _clone_version_content(conn, row, target_version, row.version)
        row.parsed_json = json.dumps(parsed, ensure_ascii=False)
        row.analysis_json = json.dumps(analysis, ensure_ascii=False)
        conn.execute(update(store.ResearchMemory).where(
            store.ResearchMemory.document_id == document_id
        ).values(active=False))
        conn.flush()
        after = document_state(row)
        change = record_change(conn, row, operation=operation, before=before, after=after)
        result = after
        result["change"] = change
    from app.research.ingest import index_document

    result["embedding_status"] = index_document(user_id, document_id)["embedding_status"]
    return result


def undo_change(user_id: str, change_id: str, *, confirmed: bool) -> dict[str, Any]:
    from app.research.service import ResearchError

    if not confirmed:
        raise ResearchError(409, "undo_confirmation_required", "撤销需要明确确认")
    with migration_session() as conn:
        row = conn.execute(select(store.ResearchChange).where(
            store.ResearchChange.id == change_id, store.ResearchChange.user_id == user_id
        )).scalar_one_or_none()
        if row is None:
            raise ResearchError(404, "change_not_found", "变更回执不存在")
        recent = conn.execute(select(store.ResearchChange.id).where(
            store.ResearchChange.user_id == user_id, store.ResearchChange.project_id == row.project_id
        ).order_by(store.ResearchChange.created_at.desc()).limit(10)).scalars().all()
        if row.id not in recent:
            raise ResearchError(409, "undo_outside_recent_window", "只能撤销最近 10 次变更")
        if row.undone_at:
            return {"change": public_change(row), "idempotent": True}
        if row.operation == "permanent_delete":
            raise ResearchError(409, "permanent_delete_not_undoable", "永久清理不能撤销")
        before = store.json_value(row.before_json, {})
        if not before or row.object_type != "document":
            raise ResearchError(409, "undo_snapshot_missing", "缺少撤销前快照")
        change_id = row.id
        document_id = row.object_id
        current_version = conn.execute(select(store.ResearchDocument.version).where(
            store.ResearchDocument.id == document_id
        )).scalar_one_or_none()
        if current_version is None:
            raise ResearchError(409, "undo_document_missing", "资料不存在，无法撤销")
    restored = restore_version(
        user_id, document_id, target_version=before["version"], expected_version=current_version,
        confirmed=True, operation="undo",
    )
    with memory_session() as conn:
        row = conn.execute(select(store.ResearchChange).where(
            store.ResearchChange.id == change_id, store.ResearchChange.user_id == user_id
        )).scalar_one()
        if row.undone_at:
            return {"change": public_change(row), "idempotent": True}
        row.undone_at = store.utc_now()
        conn.flush()
        change = public_change(row)
    return {"document": restored, "change": change, "idempotent": False}


def delete_document(user_id: str, document_id: str, *, expected_version: int, confirmed: bool) -> dict[str, Any]:
    from app.research.service import ResearchError, _document

    if not confirmed:
        raise ResearchError(409, "delete_confirmation_required", "删除资料需要明确确认")
    with memory_session() as conn:
        row = _document(conn, user_id, document_id)
        if row.version != expected_version:
            raise ResearchError(409, "document_version_changed", "资料版本已变化，请重新预览")
        if row.status == "deleted":
            result = document_state(row)
            result["idempotent"] = True
            return result
        before = document_state(row)
        snapshot(conn, row, "before_delete")
        now = datetime.now(timezone.utc)
        row.status = "deleted"
        row.approved_version = None
        row.deleted_at = now.isoformat()
        row.delete_after = (now + timedelta(days=30)).isoformat()
        row.updated_at = now.isoformat()
        conn.execute(update(store.ResearchMemory).where(
            store.ResearchMemory.document_id == document_id
        ).values(active=False))
        conn.flush()
        after = document_state(row)
        change = record_change(conn, row, operation="delete", before=before, after=after)
        result = after
        result["change"] = change
        result["idempotent"] = False
        return result


def restore_document(user_id: str, document_id: str, *, confirmed: bool) -> dict[str, Any]:
    from app.research.service import ResearchError, _document

    if not confirmed:
        raise ResearchError(409, "restore_confirmation_required", "恢复资料需要明确确认")
    with memory_session() as conn:
        row = _document(conn, user_id, document_id)
        if row.status != "deleted":
            raise ResearchError(409, "document_not_deleted", "资料不在回收站")
        previous = conn.execute(select(store.ResearchDocumentVersion).where(
            store.ResearchDocumentVersion.document_id == document_id,
            store.ResearchDocumentVersion.operation == "before_delete",
        ).order_by(store.ResearchDocumentVersion.created_at.desc())).scalars().first()
        if previous is None:
            raise ResearchError(409, "restore_snapshot_missing", "缺少删除前状态")
        before = document_state(row)
        row.status = previous.status
        row.approved_version = previous.version if previous.status == "approved" else None
        row.deleted_at = ""
        row.delete_after = ""
        row.updated_at = store.utc_now()
        conn.execute(update(store.ResearchMemory).where(
            store.ResearchMemory.document_id == document_id
        ).values(active=previous.status == "approved"))
        conn.flush()
        after = document_state(row)
        change = record_change(conn, row, operation="restore", before=before, after=after)
        result = after
        result["change"] = change
        return result


def list_recycle_bin(user_id: str, project_id: str) -> list[dict[str, Any]]:
    with migration_session() as conn:
        rows = conn.execute(select(store.ResearchDocument).where(
            store.ResearchDocument.user_id == user_id,
            store.ResearchDocument.project_id == project_id,
            store.ResearchDocument.status == "deleted",
        ).order_by(store.ResearchDocument.deleted_at.desc())).scalars().all()
        now = datetime.now(timezone.utc)
        result = []
        for row in rows:
            item = document_state(row)
            eligible = bool(row.delete_after and datetime.fromisoformat(row.delete_after) <= now)
            item["permanent_delete_eligible"] = eligible
            item["remaining_days"] = max(0, (datetime.fromisoformat(row.delete_after) - now).days)
            result.append(item)
        return result


def permanent_delete_document(user_id: str, document_id: str, *, confirmed: bool) -> dict[str, Any]:
    from app.research.service import ResearchError, _document

    if not confirmed:
        raise ResearchError(409, "permanent_delete_confirmation_required", "永久清理需要再次确认")
    with memory_session() as conn:
        row = _document(conn, user_id, document_id)
        if row.status != "deleted":
            raise ResearchError(409, "document_not_deleted", "资料不在回收站")
        if not row.delete_after or datetime.fromisoformat(row.delete_after) > datetime.now(timezone.utc):
            raise ResearchError(409, "recycle_retention_active", "回收站 30 天保留期未到期，不能永久清理")
        before = document_state(row)
        project_id, version, stored_path = row.project_id, row.version, row.stored_path
        for model in (store.ResearchEmbedding, store.ResearchMemory, store.ResearchChunk):
            conn.execute(delete(model).where(model.document_id == document_id))
        conn.execute(delete(store.ResearchDocumentVersion).where(
            store.ResearchDocumentVersion.document_id == document_id
        ))
        conn.delete(row)
        change = store.ResearchChange(
            id="chg_" + uuid.uuid4().hex, user_id=user_id, project_id=project_id,
            object_type="document", object_id=document_id, operation="permanent_delete",
            document_version=version, before_json=json.dumps(before, ensure_ascii=False),
            after_json="{}", reason="用户确认到期永久清理", created_at=store.utc_now(), undone_at="",
        )
        conn.add(change)
        conn.flush()
        change_public = public_change(change)
        change_public["undoable"] = False
    if stored_path:
        Path(stored_path).unlink(missing_ok=True)
    return {
        "document_id": document_id, "permanently_deleted": True, "system_copy_removed": True,
        "user_original_preserved": True, "change": change_public,
    }
