from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select

from app.data_access.research import memory_session
from app.research import gateway, store
from app.research.documents import parse_input
from app.research.local_components import LocalComponentError, embed_texts


def import_document(
    user_id: str,
    project_id: str,
    *,
    filename: str,
    content: bytes,
    confidentiality: str = "L2",
    document_type: str | None = None,
    source_uri: str = "",
    chunking_strategy: str = "auto",
) -> dict[str, Any]:
    from app.research.service import ResearchError, _project

    if confidentiality not in gateway.LEVELS:
        raise ResearchError(400, "invalid_confidentiality", "保密等级必须为 L1、L2 或 L3")
    safe_name = Path(filename or "").name
    if not safe_name:
        raise ResearchError(400, "invalid_filename", "文件名不能为空")
    if not content or len(content) > 100 * 1024 * 1024:
        raise ResearchError(413 if content else 400, "invalid_file_size", "文件须大于 0 字节且不超过 100 MiB")
    document_id = "doc_" + uuid.uuid4().hex
    digest = hashlib.sha256(content).hexdigest()
    title = safe_name.rsplit(".", 1)[0]
    try:
        parsed = parse_input(
            safe_name, content, document_id=document_id, title=title,
            confidentiality=confidentiality, document_type=document_type,
            chunking_strategy=chunking_strategy,
        )
    except (ValueError, UnicodeDecodeError) as exc:
        raise ResearchError(422, "document_parse_failed", f"文档解析失败：{exc}") from exc
    actual_type = document_type or parsed.document_type
    now = store.utc_now()
    owner_dir = hashlib.sha256(f"{user_id}/{project_id}".encode()).hexdigest()[:24]
    directory = store.data_dir() / "files" / owner_dir
    directory.mkdir(parents=True, exist_ok=True)
    stored_path = directory / f"{document_id}-v1-{safe_name}"
    with memory_session() as conn:
        _project(conn, user_id, project_id)
        duplicate = conn.execute(select(store.ResearchDocument).where(
            store.ResearchDocument.user_id == user_id,
            store.ResearchDocument.project_id == project_id,
            store.ResearchDocument.sha256 == digest,
        )).scalars().first()
        stored_path.write_bytes(content)
        row = store.ResearchDocument(
            id=document_id, user_id=user_id, project_id=project_id, title=parsed.card["title"],
            original_filename=safe_name, stored_path=str(stored_path), source_text=parsed.source_text,
            sha256=digest, confidentiality=confidentiality, version=1, status="pending",
            parsed_json=json.dumps(parsed.card, ensure_ascii=False), created_at=now, updated_at=now,
            document_type=actual_type, processing_status=parsed.processing_status,
            embedding_status="not_started", analysis_status="pending" if actual_type == "paper" else "not_required",
            analysis_json="{}", analysis_raw_json="", processing_error="\n".join(parsed.warnings), source_uri=source_uri,
            extraction_model="",
        )
        conn.add(row)
        conn.flush()
        for chunk in parsed.chunks:
            conn.add(store.ResearchChunk(
                id=chunk["id"], user_id=user_id, project_id=project_id, document_id=document_id,
                document_version=1, ordinal=chunk["ordinal"], chapter=chunk["chapter"],
                content=chunk["content"], confidentiality=confidentiality,
                locator_json=json.dumps(chunk.get("locator_json") or {}, ensure_ascii=False),
            ))
        conn.flush()
        result = store.public_document(row, include_text=True)
        result["sources"] = [{
            "id": chunk["id"], "document_version": 1, "ordinal": chunk["ordinal"],
            "chapter": chunk["chapter"], "content": chunk["content"],
            "confidentiality": confidentiality, "locator": chunk.get("locator_json") or {},
        } for chunk in parsed.chunks]
        result["warnings"] = parsed.warnings
        if duplicate is not None:
            result["duplicate_of"] = {
                "id": duplicate.id, "title": duplicate.title, "version": duplicate.version, "status": duplicate.status,
            }
    embedding_result = index_document(user_id, document_id)
    result["embedding_status"] = embedding_result["embedding_status"]
    if embedding_result.get("processing_error"):
        result["processing_error"] = embedding_result["processing_error"]
    return result


def index_document(user_id: str, document_id: str, *, expected_version: int | None = None) -> dict[str, Any]:
    from app.data_access.research import migration_session
    from app.research.task_control import check_cancelled
    with migration_session() as conn:
        row = conn.execute(select(store.ResearchDocument).where(
            store.ResearchDocument.id == document_id, store.ResearchDocument.user_id == user_id
        )).scalar_one_or_none()
        if row is None:
            return {"embedding_status": "missing"}
        if expected_version is not None and row.version != expected_version:
            return {"embedding_status": "stale", "processing_error": "资料版本已变化"}
        chunks = conn.execute(select(store.ResearchChunk).where(
            store.ResearchChunk.document_id == document_id,
            store.ResearchChunk.document_version == row.version,
        ).order_by(store.ResearchChunk.ordinal)).scalars().all()
        version = row.version
        needs_ocr = row.processing_status == "needs_ocr"
        contents = [chunk.content for chunk in chunks]
    check_cancelled()
    failure = "存在待 OCR 页面，向量化暂停" if needs_ocr else ""
    output = None
    if not needs_ocr:
        try:
            output = embed_texts(contents)
        except LocalComponentError as exc:
            failure = exc.message or "本地向量化失败"
    check_cancelled()
    with memory_session() as conn:
        row = conn.execute(select(store.ResearchDocument).where(
            store.ResearchDocument.id == document_id, store.ResearchDocument.user_id == user_id
        )).scalar_one_or_none()
        if row is None or row.version != version:
            return {"embedding_status": "stale", "processing_error": "资料版本已变化"}
        check_cancelled()
        if failure:
            row.embedding_status = "paused" if needs_ocr or row.confidentiality == "L3" else "degraded"
            row.processing_error = failure
            conn.flush()
            return {"embedding_status": row.embedding_status, "processing_error": failure}
        chunks = conn.execute(select(store.ResearchChunk).where(
            store.ResearchChunk.document_id == document_id, store.ResearchChunk.document_version == version,
        ).order_by(store.ResearchChunk.ordinal)).scalars().all()
        conn.execute(delete(store.ResearchEmbedding).where(
            store.ResearchEmbedding.document_id == document_id,
            store.ResearchEmbedding.document_version == row.version,
        ))
        for chunk, vector in zip(chunks, output["vectors"], strict=True):
            conn.add(store.ResearchEmbedding(
                chunk_id=chunk.id, user_id=user_id, project_id=row.project_id, document_id=document_id,
                document_version=row.version, vector_json=json.dumps(vector), model=output["model"],
                dimensions=output["dimensions"], created_at=store.utc_now(),
            ))
        row.embedding_status = "ready"
        conn.flush()
        return {"embedding_status": "ready", "model": output["model"], "count": len(chunks)}
