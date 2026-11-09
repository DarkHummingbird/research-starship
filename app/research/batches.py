from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from sqlalchemy import func, select

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
        for status in ("queued", "processing", "pending_review", "failed")
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
    if any(item.status in ("queued", "processing") for item in items):
        return
    batch = conn.scalar(select(store.ResearchBatch).where(store.ResearchBatch.id == batch_id))
    batch.status = "completed_with_errors" if any(item.status == "failed" for item in items) else "completed"
    batch.completed_at = store.utc_now()


def _run_document(user_id: str, document_id: str) -> None:
    from app.research.service import ResearchError, get_document

    document = get_document(user_id, document_id)
    if document["status"] not in ("pending", "rejected"):
        raise ResearchError(409, "batch_document_not_pending", "资料不在可处理状态")
    if document["processing_status"] == "needs_ocr":
        from app.research.ocr import ocr_document

        ocr_document(user_id, document_id)
    elif document["document_type"] == "paper" and document["analysis_status"] in ("pending", "failed"):
        from app.research.paper_analysis import analyze_document

        analyze_document(user_id, document_id)


def _process_item(item_id: str) -> None:
    from app.data_access import system_scope

    with system_scope():
        _process_item_scoped(item_id)


def _process_item_scoped(item_id: str) -> None:
    with memory_session() as conn:
        item = conn.execute(select(store.ResearchBatchItem).where(
            store.ResearchBatchItem.id == item_id
        )).scalar_one_or_none()
        if item is None or item.status != "queued":
            return
        item.status = "processing"
        item.started_at = store.utc_now()
        user_id, document_id, batch_id = item.user_id, item.document_id, item.batch_id
        conn.flush()
    try:
        _run_document(user_id, document_id)
    except Exception as exc:
        with memory_session() as conn:
            item = conn.execute(select(store.ResearchBatchItem).where(
                store.ResearchBatchItem.id == item_id
            )).scalar_one()
            item.status = "failed"
            item.error = f"{type(exc).__name__}: {exc}"[:1500]
            item.completed_at = store.utc_now()
            _finish_batch(conn, batch_id)
        return
    with memory_session() as conn:
        item = conn.execute(select(store.ResearchBatchItem).where(
            store.ResearchBatchItem.id == item_id
        )).scalar_one()
        item.status = "pending_review"
        item.completed_at = store.utc_now()
        _finish_batch(conn, batch_id)


def create_batch(user_id: str, project_id: str, document_ids: list[str]) -> dict[str, Any]:
    from app.research.service import ResearchError, _document, _project

    document_ids = list(dict.fromkeys(document_ids))
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
            if document.project_id != project_id or document.status not in ("pending", "rejected"):
                raise ResearchError(409, "batch_document_not_pending", "批次只接受当前课题的待处理资料")
            item_id = "bti_" + uuid.uuid4().hex
            item_ids.append(item_id)
            conn.add(store.ResearchBatchItem(
                id=item_id, batch_id=batch_id, user_id=user_id, project_id=project_id,
                document_id=document_id, status="queued", error="", created_at=now,
                started_at="", completed_at="",
            ))
        conn.flush()
    for item_id in item_ids:
        _executor_pool().submit(_process_item, item_id)
    return get_batch(user_id, batch_id)


def retry_batch_item(user_id: str, item_id: str) -> dict[str, Any]:
    from app.research.service import ResearchError

    with memory_session() as conn:
        item = conn.execute(select(store.ResearchBatchItem).where(
            store.ResearchBatchItem.id == item_id, store.ResearchBatchItem.user_id == user_id
        )).scalar_one_or_none()
        if item is None:
            raise ResearchError(404, "batch_item_not_found", "批次条目不存在")
        if item.status != "failed":
            raise ResearchError(409, "batch_item_not_failed", "只有失败条目允许手动重试")
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
            store.ResearchBatchItem.status == "processing"
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
