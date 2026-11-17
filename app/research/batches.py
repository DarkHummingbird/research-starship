from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from sqlalchemy import func, select, update

from app.data_access.research import memory_session, migration_session
from app.research import store


_executor_lock = threading.Lock()
_executor: ThreadPoolExecutor | None = None


def _executor_pool() -> ThreadPoolExecutor:
    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="research-batch")
        return _executor


def _public_batch(row, items: list) -> dict[str, Any]:
    item = store.model_dict(row)
    item.pop("user_id", None)
    item["items"] = []
    for entry in items:
        value = store.model_dict(entry)
        value.pop("user_id", None)
        item["items"].append(value)
    item["summary"] = {
        status: sum(1 for entry in items if entry.status == status)
        for status in ("queued", "processing", "cancelling", "awaiting_approval", "pending_review", "failed", "cancelled")
    }
    return item


def get_batch(user_id: str, batch_id: str) -> dict[str, Any]:
    from app.research.service import ResearchError

    with migration_session() as conn:
        row = conn.execute(select(store.ResearchBatch).where(
            store.ResearchBatch.id == batch_id, store.ResearchBatch.user_id == user_id
        )).scalar_one_or_none()
        if row is None:
            raise ResearchError(404, "batch_not_found", "批次不存在")
        items = conn.execute(select(store.ResearchBatchItem).where(
            store.ResearchBatchItem.batch_id == batch_id
        ).order_by(store.ResearchBatchItem.created_at)).scalars().all()
        return _public_batch(row, items)


def _finish_batch(conn, batch_id: str) -> None:
    items = conn.scalars(select(store.ResearchBatchItem).where(
        store.ResearchBatchItem.batch_id == batch_id
    )).all()
    if any(item.status in ("queued", "processing", "cancelling", "awaiting_approval") for item in items):
        return
    batch = conn.scalar(select(store.ResearchBatch).where(store.ResearchBatch.id == batch_id))
    batch.status = ("completed_with_errors" if any(item.status == "failed" for item in items)
                    else "cancelled" if all(item.status == "cancelled" for item in items)
                    else "completed")
    batch.completed_at = store.utc_now()


def _run_document(
    user_id: str,
    document_id: str,
    expected_version: int = 0,
    operation: str = "analysis",
    task_id: str = "",
    approval_id: str = "",
):
    from app.research.service import ResearchError, get_document

    document = get_document(user_id, document_id)
    if expected_version and document["version"] != expected_version:
        raise ResearchError(409, "document_version_changed", "资料版本已变化，请新建任务")
    if operation == "reindex":
        if document["status"] == "deleted" or document["processing_status"] != "ready":
            raise ResearchError(409, "index_document_unavailable", "资料当前不能重建索引")
        from app.research.ingest import index_document
        result = index_document(user_id, document_id, expected_version=expected_version or None)
        if result.get("embedding_status") == "stale":
            raise ResearchError(409, "document_version_changed", "资料版本已变化，请新建任务")
        if result.get("embedding_status") != "ready":
            raise ResearchError(503, "local_embedding_unavailable", "本地检索模型不可用，请检查后手动重试")
        return
    if document["status"] not in ("pending", "rejected"):
        raise ResearchError(409, "batch_document_not_pending", "资料不在可处理状态")
    if document["processing_status"] == "needs_ocr":
        from app.research.ocr import ocr_document

        return ocr_document(
            user_id, document_id, expected_version=expected_version or None,
            provider="external" if operation == "ocr_online" else "local",
            task_id=task_id, approval_id=approval_id,
        )
    elif document["document_type"] == "paper" and (operation == "reanalyze" or document["analysis_status"] in ("pending", "failed")):
        from app.research.paper_analysis import analyze_document

        return analyze_document(
            user_id, document_id, expected_version=expected_version or None,
            provider="rag" if document["confidentiality"] == "L3" else "external",
            task_id=task_id, approval_id=approval_id,
        )


def _process_item(item_id: str) -> None:
    from app.data_access import system_scope
    from app.research.task_control import task_scope

    with system_scope(), task_scope(item_id):
        _process_item_scoped(item_id)


def _process_item_scoped(item_id: str) -> None:
    with memory_session() as conn:
        claimed = conn.execute(update(store.ResearchBatchItem).where(
            store.ResearchBatchItem.id == item_id, store.ResearchBatchItem.status == "queued"
        ).values(status="processing", started_at=store.utc_now()))
        if claimed.rowcount != 1:
            return
        item = conn.execute(select(store.ResearchBatchItem).where(store.ResearchBatchItem.id == item_id)).scalar_one()
        batch = conn.execute(select(store.ResearchBatch).where(store.ResearchBatch.id == item.batch_id)).scalar_one()
        batch.status = "processing"
        user_id, document_id, batch_id, version, operation = item.user_id, item.document_id, item.batch_id, item.document_version or 0, item.operation or "analysis"
        approval_id = item.approval_id or ""
        conn.flush()
    try:
        from app.research.task_control import check_cancelled
        check_cancelled()
        result = _run_document(user_id, document_id, version, operation, item_id, approval_id)
    except Exception as exc:
        from app.research.task_control import TaskCancelled
        with memory_session() as conn:
            item = conn.execute(select(store.ResearchBatchItem).where(
                store.ResearchBatchItem.id == item_id
            )).scalar_one()
            item.status = "cancelled" if isinstance(exc, TaskCancelled) else "failed"
            item.error = "已停止，已完成的步骤可能保留" if isinstance(exc, TaskCancelled) else f"{type(exc).__name__}: {exc}"[:1500]
            item.completed_at = store.utc_now()
            _finish_batch(conn, batch_id)
        return
    with memory_session() as conn:
        item = conn.execute(select(store.ResearchBatchItem).where(
            store.ResearchBatchItem.id == item_id
        )).scalar_one()
        if isinstance(result, dict) and result.get("status") == "awaiting_outbound_approval":
            item.status = "awaiting_approval"
            item.approval_id = result["approval"]["id"]
            item.completed_at = ""
        else:
            item.status = "pending_review"
            item.completed_at = store.utc_now()
            document = conn.execute(select(store.ResearchDocument).where(
                store.ResearchDocument.id == item.document_id
            )).scalar_one_or_none()
            from app.agent.proactive.store import create_item
            create_item(
                user_id, "research_review",
                title="有资料等待你确认",
                body=f"《{document.title if document else item.document_id}》已处理完成，请到科研星舰的“待我确认”核对后再用于问答。",
                payload={"document_id": item.document_id, "project_id": item.project_id, "batch_item_id": item.id},
                period_key=f"{item.document_id}:v{item.document_version}",
            )
        _finish_batch(conn, batch_id)


def create_batch(user_id: str, project_id: str, document_ids: list[str], *, operation: str = "analysis") -> dict[str, Any]:
    from app.research.service import ResearchError, _document, _project

    document_ids = list(dict.fromkeys(document_ids))
    if operation not in ("analysis", "reanalyze", "reindex", "ocr_online"):
        raise ResearchError(400, "invalid_task_operation", "任务类型无效")
    if not 1 <= len(document_ids) <= 5:
        raise ResearchError(409, "batch_split_required", "单批须为 1–5 篇；超过 5 篇请确认拆批")
    batch_id = "bat_" + uuid.uuid4().hex
    item_ids = []
    with memory_session() as conn:
        _project(conn, user_id, project_id)
        now = store.utc_now()
        batch = store.ResearchBatch(
            id=batch_id, user_id=user_id, project_id=project_id, status="queued",
            item_count=len(document_ids), created_at=now, completed_at="",
        )
        conn.add(batch)
        for document_id in document_ids:
            document = _document(conn, user_id, document_id)
            allowed = document.status in ("pending", "rejected") if operation in ("analysis", "reanalyze", "ocr_online") else document.status != "deleted" and document.processing_status == "ready"
            if operation == "reanalyze":
                allowed = allowed and document.document_type == "paper" and document.processing_status == "ready"
            if operation == "ocr_online":
                allowed = allowed and document.processing_status == "needs_ocr"
            if document.project_id != project_id or not allowed:
                raise ResearchError(409, "batch_document_not_pending", "批次只接受当前课题的待处理资料")
            active = conn.execute(select(store.ResearchBatchItem.id).where(
                store.ResearchBatchItem.user_id == user_id, store.ResearchBatchItem.document_id == document_id,
                store.ResearchBatchItem.status.in_(("queued", "processing", "cancelling"))
            ).limit(1)).scalar_one_or_none()
            if active:
                raise ResearchError(409, "document_task_running", "这份资料已有排队或正在处理的任务")
            item_id = "bti_" + uuid.uuid4().hex
            item_ids.append(item_id)
            conn.add(store.ResearchBatchItem(
                id=item_id, batch_id=batch_id, user_id=user_id, project_id=project_id,
                document_id=document_id, document_version=document.version, operation=operation, status="queued", error="", created_at=now,
                started_at="", completed_at="",
            ))
        conn.flush()
    for item_id in item_ids:
        _executor_pool().submit(_process_item, item_id)
    return get_batch(user_id, batch_id)


def retry_batch_item(user_id: str, item_id: str, *, approval_id: str = "") -> dict[str, Any]:
    from app.research.service import ResearchError, _document
    from app.research import gateway

    with memory_session() as conn:
        item = conn.execute(select(store.ResearchBatchItem).where(
            store.ResearchBatchItem.id == item_id, store.ResearchBatchItem.user_id == user_id
        )).scalar_one_or_none()
        if item is None:
            raise ResearchError(404, "batch_item_not_found", "批次条目不存在")
        if item.status not in ("failed", "awaiting_approval"):
            raise ResearchError(409, "batch_item_not_failed", "只有失败或待审批条目允许继续处理")
        if item.status == "awaiting_approval":
            if not approval_id or approval_id != item.approval_id:
                raise ResearchError(409, "approval_scope_changed", "请使用当前任务展示的审批继续分析")
            gateway.get(user_id, approval_id)
        else:
            item.approval_id = ""
        if not item.document_version:
            raise ResearchError(409, "task_version_unknown", "旧任务没有记录资料版本，请重新提交任务")
        if _document(conn, user_id, item.document_id).version != item.document_version:
            raise ResearchError(409, "document_version_changed", "资料版本已变化，请重新提交任务")
        active = conn.execute(select(store.ResearchBatchItem.id).where(
            store.ResearchBatchItem.user_id == user_id, store.ResearchBatchItem.document_id == item.document_id,
            store.ResearchBatchItem.id != item_id, store.ResearchBatchItem.status.in_(("queued", "processing", "cancelling"))
        ).limit(1)).scalar_one_or_none()
        if active:
            raise ResearchError(409, "document_task_running", "这份资料已有排队或正在处理的任务")
        item.status = "queued"
        item.error = ""
        item.started_at = ""
        item.completed_at = ""
        batch_id = item.batch_id
        batch = conn.execute(select(store.ResearchBatch).where(store.ResearchBatch.id == batch_id)).scalar_one()
        batch.status = "queued"
        batch.completed_at = ""
        conn.flush()
    _executor_pool().submit(_process_item, item_id)
    return get_batch(user_id, batch_id)


def recover_interrupted_batches() -> int:
    from app.data_access import system_scope

    with system_scope(), memory_session() as conn:
        items = conn.execute(select(store.ResearchBatchItem).where(
            store.ResearchBatchItem.status.in_(("queued", "processing", "cancelling"))
        )).scalars().all()
        for item in items:
            item.status = "failed"
            item.error = "服务重启中断；请用户手动重试"
            item.completed_at = store.utc_now()
            _finish_batch(conn, item.batch_id)
        return len(items)


def list_batches(user_id: str, project_id: str) -> list[dict[str, Any]]:
    with migration_session() as conn:
        rows = conn.execute(select(store.ResearchBatch).where(
            store.ResearchBatch.user_id == user_id, store.ResearchBatch.project_id == project_id
        ).order_by(store.ResearchBatch.created_at.desc())).scalars().all()
        return [get_batch(user_id, row.id) for row in rows]


def list_tasks(user_id: str) -> list[dict[str, Any]]:
    with migration_session() as conn:
        active = conn.execute(select(store.ResearchBatch).where(
            store.ResearchBatch.user_id == user_id, store.ResearchBatch.status.in_(("queued", "processing", "awaiting_approval"))
        ).order_by(store.ResearchBatch.created_at.desc())).scalars().all()
        recent = conn.execute(select(store.ResearchBatch).where(
            store.ResearchBatch.user_id == user_id, store.ResearchBatch.status.not_in(("queued", "processing", "awaiting_approval"))
        ).order_by(store.ResearchBatch.created_at.desc()).limit(100)).scalars().all()
        batches = [*active, *recent]
        output = []
        for batch in batches:
            items = conn.execute(select(store.ResearchBatchItem).where(
                store.ResearchBatchItem.batch_id == batch.id, store.ResearchBatchItem.user_id == user_id
            ).order_by(store.ResearchBatchItem.created_at)).scalars().all()
            value = _public_batch(batch, items)
            project = conn.execute(select(store.ResearchProject).where(
                store.ResearchProject.id == batch.project_id, store.ResearchProject.user_id == user_id
            )).scalar_one_or_none()
            value["project_name"] = project.name if project else "课题已不存在"
            for item in value["items"]:
                document = conn.execute(select(store.ResearchDocument).where(
                    store.ResearchDocument.id == item["document_id"], store.ResearchDocument.user_id == user_id
                )).scalar_one_or_none()
                item["document_title"] = document.title if document else "资料已不存在"
            output.append(value)
        return output


def cancel_batch_item(user_id: str, item_id: str) -> dict[str, Any]:
    from app.research.service import ResearchError
    from app.research.task_control import request_cancel
    signalled = False
    with migration_session() as conn:
        existing = conn.execute(select(store.ResearchBatchItem).where(
            store.ResearchBatchItem.id == item_id, store.ResearchBatchItem.user_id == user_id
        )).scalar_one_or_none()
        if existing is None:
            raise ResearchError(404, "batch_item_not_found", "任务不存在")
        if existing.status == "processing":
            if not request_cancel(item_id):
                raise ResearchError(409, "task_not_cancellable", "任务状态正在变化，请刷新后查看")
            signalled = True
    with memory_session() as conn:
        item = conn.execute(select(store.ResearchBatchItem).where(
            store.ResearchBatchItem.id == item_id, store.ResearchBatchItem.user_id == user_id
        )).scalar_one_or_none()
        if item is None:
            raise ResearchError(404, "batch_item_not_found", "任务不存在")
        if item.status == "cancelled" and signalled:
            return _public_batch(conn.scalar(select(store.ResearchBatch).where(store.ResearchBatch.id == item.batch_id)),
                list(conn.scalars(select(store.ResearchBatchItem).where(store.ResearchBatchItem.batch_id == item.batch_id)).all()))
        if item.status == "processing":
            item.status = "cancelling"
            conn.flush()
            batch_id = item.batch_id
            return _public_batch(conn.scalar(select(store.ResearchBatch).where(store.ResearchBatch.id == batch_id)),
                list(conn.scalars(select(store.ResearchBatchItem).where(store.ResearchBatchItem.batch_id == batch_id)).all()))
        claimed = conn.execute(update(store.ResearchBatchItem).where(
            store.ResearchBatchItem.id == item_id, store.ResearchBatchItem.user_id == user_id,
            store.ResearchBatchItem.status == "queued"
        ).values(status="cancelled", completed_at=store.utc_now()))
        if claimed.rowcount != 1:
            raise ResearchError(409, "task_not_cancellable", "任务已完成或状态发生变化，请刷新查看")
        conn.flush()
        batch_id = item.batch_id
        _finish_batch(conn, batch_id)
    return get_batch(user_id, batch_id)
