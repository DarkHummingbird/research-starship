from __future__ import annotations

import hashlib
import json
import re
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select

from app.data_access.research import memory_session, migration_session
from app.research import store
from app.research.documents import _card, _split_text
from app.research.local_components import LocalComponentError, ocr_image


def has_formula_notation(text: str) -> bool:
    return bool(re.search(r"[Σ∑√]|log\s*\(|softmax|\^|_", text, re.I))


def ocr_document(
    user_id: str,
    document_id: str,
    *,
    expected_version: int | None = None,
    provider: str = "local",
    task_id: str = "",
    approval_id: str = "",
) -> dict[str, Any]:
    from app.research.service import ResearchError, _document
    from app.research.ingest import index_document
    from app.research.task_control import check_cancelled
    from app.research.local_mode import require_enabled

    if provider not in ("local", "external"):
        raise ResearchError(400, "invalid_ocr_provider", "OCR 通道必须为本地或 DeepSeek 在线识别")
    if provider == "local":
        require_enabled(user_id)
    with migration_session() as conn:
        row = _document(conn, user_id, document_id)
        if expected_version is not None and row.version != expected_version:
            raise ResearchError(409, "document_version_changed", "资料版本已变化，请新建任务")
        if Path(row.original_filename).suffix.lower() != ".pdf":
            raise ResearchError(400, "ocr_pdf_only", "OCR 仅适用于 PDF 页面")
        if row.status == "approved":
            raise ResearchError(409, "approved_document_requires_new_import", "已确认资料请重新导入新版本后再 OCR")
        source = {
            "path": row.stored_path, "version": row.version, "title": row.title,
            "confidentiality": row.confidentiality, "project_id": row.project_id,
            "original_filename": row.original_filename,
        }
    online_pages: dict[int, dict[str, Any]] = {}
    online_usage: dict[str, Any] = {}
    if provider == "external":
        from app.research import gateway
        from app.research.online_ocr import ocr_spec, recognize

        spec = ocr_spec()
        if not spec.has_key():
            raise ResearchError(400, "online_ocr_unconfigured", "DeepSeek OCR 未配置，不会自动切换本地或其他服务商")
        path = Path(source["path"])
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        task = task_id or "task_" + uuid.uuid4().hex
        fragments = [{
            "document_id": document_id, "version": source["version"], "chunk_id": "",
            "chapter": "原始 PDF 整份文件",
            "snippet": f"DeepSeek OCR 将接收完整原始 PDF：{source['original_filename']}；SHA-256：{digest}",
            "classification": source["confidentiality"], "sha256": digest,
        }]
        approval = gateway.prepare(
            user_id, source["project_id"], task_id=task, provider="deepseek", model_id=spec.id,
            destination=spec.base_url, purpose="DeepSeek 在线 OCR", operation="ocr", fragments=fragments,
        )
        if approval["status"] == "pending":
            return {"status": "awaiting_outbound_approval", "approval": approval,
                    "task_id": task, "document_id": document_id}
        if approval_id and approval["id"] != approval_id:
            raise ResearchError(409, "approval_scope_changed", "OCR 文件、版本或用途已变化，请重新审批")
        recognized = gateway.execute_once(
            user_id, approval["id"], lambda _approval: recognize(path),
        )
        online_pages = {page["index"] + 1: page for page in recognized["pages"]}
        online_usage = recognized.get("usage") or {}
    try:
        import fitz
    except ImportError as exc:
        raise ResearchError(503, "pdf_renderer_unavailable", "本地 PDF 渲染组件 PyMuPDF 不可用") from exc
    ocr_pages: list[int] = []
    low_confidence: list[int] = []
    formula_pages: list[int] = []
    incomplete_pages: list[int] = []
    sections: list[tuple[str, str, dict[str, Any]]] = []
    source_parts: list[str] = []
    try:
        pdf = fitz.open(source["path"])
        if provider == "external" and len(online_pages) != len(pdf):
            raise ResearchError(502, "online_ocr_invalid", "DeepSeek OCR 返回页数与原文件不一致；未自动重试")
        output_dir = store.data_dir() / "ocr" / document_id
        output_dir.mkdir(parents=True, exist_ok=True)
        for page_number, page in enumerate(pdf, 1):
            check_cancelled()
            text = (page.get_text() or "").strip()
            extraction = "pdf_text"
            ocr_lines = []
            if not text:
                if provider == "external":
                    from app.research.online_ocr import parse_page

                    output = online_pages[page_number]
                    text, ocr_lines = parse_page(output["text"], page)
                    extraction = "deepseek_ocr"
                    if output["finish_reason"] == "length":
                        incomplete_pages.append(page_number)
                else:
                    image_path = output_dir / f"page-{page_number}.png"
                    page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False).save(str(image_path))
                    output = ocr_image(image_path)
                    text = "\n".join(output["texts"]).strip()
                    boxes = output.get("boxes", [])
                    if len(boxes) == len(output["texts"]):
                        ocr_lines = [{"text": line, "box": [float(value) / 2 for value in box]}
                                     for line, box in zip(output["texts"], boxes, strict=True)
                                     if isinstance(box, list) and len(box) == 4]
                    extraction = "offline_ocr"
                    if any(score < 0.75 for score in output.get("scores", [])):
                        low_confidence.append(page_number)
                ocr_pages.append(page_number)
            if has_formula_notation(text):
                formula_pages.append(page_number)
            if not text:
                text = "DeepSeek 在线 OCR 未识别出可核验文字，请人工查看原页。" if provider == "external" else "本地 OCR 未识别出可核验文字，请人工查看原页。"
            for index, part in enumerate(_split_text(text), 1):
                chapter = f"第 {page_number} 页" + (f"（片段 {index}）" if index > 1 else "")
                locator = {"page": page_number, "part": index, "extraction": extraction}
                if ocr_lines:
                    locator["ocr_lines"] = ocr_lines
                sections.append((chapter, part, locator))
                source_parts.append(f"[{chapter}｜{extraction}]\n{part}")
        pdf.close()
    except LocalComponentError as exc:
        with memory_session() as conn:
            row = _document(conn, user_id, document_id)
            row.processing_status = "failed"
            row.processing_error = exc.message
            conn.flush()
        raise ResearchError(503, "local_ocr_failed", f"本地 OCR 失败，未自动重试：{exc.message}") from exc
    new_version = source["version"] + 1
    check_cancelled()
    card, chunks = _card(
        document_id=document_id, version=new_version, title=source["title"],
        confidentiality=source["confidentiality"], sections=sections, document_type="paper",
    )
    with memory_session() as conn:
        check_cancelled()
        row = _document(conn, user_id, document_id)
        if row.version != source["version"]:
            raise ResearchError(409, "document_version_changed", "OCR 期间资料版本已变化，请重新检查")
        conn.execute(delete(store.ResearchMemory).where(store.ResearchMemory.document_id == document_id))
        conn.execute(delete(store.ResearchEmbedding).where(store.ResearchEmbedding.document_id == document_id))
        row.version = new_version
        row.status = "pending"
        row.approved_version = None
        row.processing_status = "ready"
        row.embedding_status = "not_started"
        row.analysis_status = "pending"
        row.analysis_json = "{}"
        row.analysis_raw_json = ""
        row.parsed_json = json.dumps(card, ensure_ascii=False)
        row.source_text = "\n\n".join(source_parts)
        warnings = []
        if low_confidence:
            warnings.append(f"低置信 OCR 页：{low_confidence}")
        if formula_pages:
            warnings.append(f"检测到公式/上下标页：{formula_pages}；请人工核对符号，OCR 置信度不代表公式正确")
        if incomplete_pages:
            warnings.append(f"DeepSeek OCR 输出达到长度上限的页：{incomplete_pages}；这些页内容不完整，必须对照原文补充")
        row.processing_error = "；".join(warnings)
        row.updated_at = store.utc_now()
        for chunk in chunks:
            conn.add(store.ResearchChunk(
                id=chunk["id"], user_id=user_id, project_id=row.project_id, document_id=document_id,
                document_version=new_version, ordinal=chunk["ordinal"], chapter=chunk["chapter"],
                content=chunk["content"], confidentiality=row.confidentiality,
                locator_json=json.dumps(chunk.get("locator_json") or {}, ensure_ascii=False),
            ))
        conn.flush()
        result = store.public_document(row, include_text=True)
        result["sources"] = [{
            "id": chunk["id"], "document_version": new_version, "ordinal": chunk["ordinal"],
            "chapter": chunk["chapter"], "content": chunk["content"],
            "confidentiality": row.confidentiality, "locator": chunk.get("locator_json") or {},
        } for chunk in chunks]
        result["ocr_pages"] = ocr_pages
        result["low_confidence_pages"] = low_confidence
        result["formula_pages"] = formula_pages
        result["incomplete_pages"] = incomplete_pages
        result["ocr_provider"] = "external:deepseek-ocr" if provider == "external" else "local"
        if provider == "external":
            result["ocr_usage"] = online_usage
    result.update(index_document(user_id, document_id))
    return result
