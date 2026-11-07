from __future__ import annotations

import hashlib
import json
import re
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, select, update

from app.data_access.research import memory_session, migration_session
from app.research import gateway, store
from app.research.parser import parse_markdown, validate_document_card


class ResearchError(ValueError):
    def __init__(self, status: int, code: str, message: str, **data: Any):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.data = data


def _id(prefix: str) -> str:
    return prefix + uuid.uuid4().hex


def _project(conn, user_id: str, project_id: str):
    row = conn.execute(select(store.ResearchProject).where(
        store.ResearchProject.id == project_id, store.ResearchProject.user_id == user_id
    )).scalar_one_or_none()
    if row is None:
        raise ResearchError(404, "project_not_found", "课题不存在或不属于当前用户")
    return row


def _document(conn, user_id: str, document_id: str):
    row = conn.execute(select(store.ResearchDocument).where(
        store.ResearchDocument.id == document_id, store.ResearchDocument.user_id == user_id
    )).scalar_one_or_none()
    if row is None:
        raise ResearchError(404, "document_not_found", "资料不存在或不属于当前用户")
    return row


def create_project(user_id: str, name: str, goal: str = "", notes: str = "") -> dict[str, Any]:
    name = name.strip()
    if not name or len(name) > 120:
        raise ResearchError(400, "invalid_project_name", "课题名称须为 1–120 个字符")
    project_id = _id("prj_")
    now = store.utc_now()
    with memory_session() as conn:
        row = store.ResearchProject(
            id=project_id, user_id=user_id, name=name, goal=goal.strip()[:4000],
            notes=notes.strip()[:4000], created_at=now, updated_at=now,
        )
        conn.add(row)
        conn.flush()
        return store.model_dict(row)


def list_projects(user_id: str) -> list[dict[str, Any]]:
    with migration_session() as conn:
        rows = conn.execute(select(store.ResearchProject).where(
            store.ResearchProject.user_id == user_id
        ).order_by(store.ResearchProject.created_at)).scalars().all()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = store.model_dict(row)
            item["document_count"] = conn.scalar(select(func.count(store.ResearchDocument.id)).where(
                store.ResearchDocument.project_id == row.id
            ))
            item["pending_count"] = conn.scalar(select(func.count(store.ResearchDocument.id)).where(
                store.ResearchDocument.project_id == row.id, store.ResearchDocument.status == "pending"
            ))
            item["memory_count"] = conn.scalar(select(func.count(store.ResearchMemory.id)).where(
                store.ResearchMemory.project_id == row.id
            ))
            result.append(item)
        return result


def import_markdown(
    user_id: str,
    project_id: str,
    *,
    filename: str,
    content: bytes,
    confidentiality: str = "L2",
) -> dict[str, Any]:
    if confidentiality not in gateway.LEVELS:
        raise ResearchError(400, "invalid_confidentiality", "保密等级必须为 L1、L2 或 L3")
    safe_name = Path(filename or "").name
    if not safe_name or Path(safe_name).suffix.lower() not in (".md", ".markdown"):
        raise ResearchError(415, "markdown_only", "P1 仅支持 Markdown（.md/.markdown）资料")
    if not content or len(content) > 100 * 1024 * 1024:
        raise ResearchError(413 if content else 400, "invalid_file_size", "文件须大于 0 字节且不超过 100 MiB")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ResearchError(422, "invalid_utf8", "Markdown 须使用 UTF-8 编码") from exc
    document_id = _id("doc_")
    digest = hashlib.sha256(content).hexdigest()
    title = safe_name.rsplit(".", 1)[0]
    parsed, chunks = parse_markdown(
        text, document_id=document_id, version=1, title=title, confidentiality=confidentiality
    )
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
            id=document_id, user_id=user_id, project_id=project_id, title=parsed["title"],
            original_filename=safe_name, stored_path=str(stored_path), source_text=text, sha256=digest,
            confidentiality=confidentiality, version=1, status="pending",
            parsed_json=json.dumps(parsed, ensure_ascii=False), created_at=now, updated_at=now,
        )
        conn.add(row)
        conn.flush()
        for chunk in chunks:
            conn.add(store.ResearchChunk(
                id=chunk["id"], user_id=user_id, project_id=project_id, document_id=document_id,
                document_version=1, ordinal=chunk["ordinal"], chapter=chunk["chapter"],
                content=chunk["content"], confidentiality=confidentiality,
            ))
        conn.flush()
        result = store.public_document(row, include_text=True)
        if duplicate is not None:
            result["duplicate_of"] = {
                "id": duplicate.id, "title": duplicate.title, "version": duplicate.version, "status": duplicate.status,
            }
        return result


def list_documents(user_id: str, project_id: str) -> list[dict[str, Any]]:
    with migration_session() as conn:
        _project(conn, user_id, project_id)
        rows = conn.execute(select(store.ResearchDocument).where(
            store.ResearchDocument.user_id == user_id, store.ResearchDocument.project_id == project_id
        ).order_by(store.ResearchDocument.created_at.desc())).scalars().all()
        return [store.public_document(row) for row in rows]


def get_document(user_id: str, document_id: str) -> dict[str, Any]:
    with migration_session() as conn:
        row = _document(conn, user_id, document_id)
        item = store.public_document(row, include_text=True)
        sources = conn.execute(select(store.ResearchChunk).where(
            store.ResearchChunk.document_id == document_id,
            store.ResearchChunk.document_version == row.version,
        ).order_by(store.ResearchChunk.ordinal)).scalars().all()
        item["sources"] = [{
            "id": source.id, "document_version": source.document_version, "ordinal": source.ordinal,
            "chapter": source.chapter, "content": source.content, "confidentiality": source.confidentiality,
        } for source in sources]
        item["system_copy_sha256"] = hashlib.sha256(Path(row.stored_path).read_bytes()).hexdigest()
        item["system_copy_matches_original"] = item["system_copy_sha256"] == row.sha256
        return item


def update_document(
    user_id: str,
    document_id: str,
    *,
    expected_version: int,
    title: str | None = None,
    confidentiality: str | None = None,
) -> dict[str, Any]:
    if confidentiality is not None and confidentiality not in gateway.LEVELS:
        raise ResearchError(400, "invalid_confidentiality", "保密等级必须为 L1、L2 或 L3")
    with memory_session() as conn:
        row = _document(conn, user_id, document_id)
        if row.version != expected_version:
            raise ResearchError(409, "document_version_changed", "资料版本已变化，请重新预览")
        new_title = (title if title is not None else row.title).strip()
        if not new_title or len(new_title) > 300:
            raise ResearchError(400, "invalid_document_title", "资料标题须为 1–300 个字符")
        new_level = confidentiality or row.confidentiality
        new_version = row.version + 1
        parsed = store.json_value(row.parsed_json, {})
        parsed["title"] = new_title
        old_chunks = conn.execute(select(store.ResearchChunk).where(
            store.ResearchChunk.document_id == document_id,
            store.ResearchChunk.document_version == row.version,
        ).order_by(store.ResearchChunk.ordinal)).scalars().all()
        chunks = [store.model_dict(chunk) for chunk in old_chunks]
        for chunk in chunks:
            chunk["document_version"] = new_version
            chunk["id"] = f"{document_id}:v{new_version}:c{chunk['ordinal']}"
            chunk["confidentiality"] = new_level
        for ordinal, entry in enumerate(parsed.get("entries") or [], 1):
            entry["confidentiality"] = new_level
            entry["review_status"] = "pending"
            entry["evidence"]["version"] = new_version
            entry["evidence"]["chunk_id"] = f"{document_id}:v{new_version}:c{ordinal}"
        validate_document_card(parsed, chunks)
        conn.execute(delete(store.ResearchMemory).where(store.ResearchMemory.document_id == document_id))
        row.title = new_title
        row.confidentiality = new_level
        row.version = new_version
        row.status = "pending"
        row.parsed_json = json.dumps(parsed, ensure_ascii=False)
        row.approved_version = None
        row.updated_at = store.utc_now()
        for chunk in chunks:
            conn.add(store.ResearchChunk(
                id=chunk["id"], user_id=user_id, project_id=chunk["project_id"], document_id=document_id,
                document_version=new_version, ordinal=chunk["ordinal"], chapter=chunk["chapter"],
                content=chunk["content"], confidentiality=new_level,
            ))
        conn.flush()
        return store.public_document(row, include_text=True)


def review_document(user_id: str, document_id: str, *, expected_version: int, approved: bool) -> dict[str, Any]:
    with memory_session() as conn:
        row = _document(conn, user_id, document_id)
        if row.version != expected_version:
            raise ResearchError(409, "document_version_changed", "资料版本已变化，原审核预览失效")
        if approved and row.status == "approved" and row.approved_version == expected_version:
            result = store.public_document(row)
            result["idempotent"] = True
            return result
        if not approved and row.status == "rejected":
            result = store.public_document(row)
            result["idempotent"] = True
            return result
        now = store.utc_now()
        if not approved:
            conn.execute(delete(store.ResearchMemory).where(store.ResearchMemory.document_id == document_id))
            row.status = "rejected"
            row.approved_version = None
            row.updated_at = now
        else:
            parsed = store.json_value(row.parsed_json, {})
            chunks = conn.execute(select(store.ResearchChunk).where(
                store.ResearchChunk.document_id == document_id,
                store.ResearchChunk.document_version == expected_version,
            ).order_by(store.ResearchChunk.ordinal)).scalars().all()
            chunk_dicts = [store.model_dict(chunk) for chunk in chunks]
            try:
                validate_document_card(parsed, chunk_dicts)
            except ValueError as exc:
                raise ResearchError(422, "invalid_extraction", f"结构化结果或来源校验失败：{exc}") from exc
            by_chunk = {chunk.id: chunk for chunk in chunks}
            for entry in parsed["entries"]:
                evidence = entry["evidence"]
                chunk = by_chunk[evidence["chunk_id"]]
                conn.add(store.ResearchMemory(
                    id=_id("mem_"), user_id=user_id, project_id=row.project_id, document_id=document_id,
                    document_version=expected_version, chunk_id=chunk.id, entry_id=entry["entry_id"],
                    title=parsed["title"], statement=entry["statement"], chapter=chunk.chapter,
                    record_type=parsed["record_type"], confidentiality=row.confidentiality,
                    review_status="confirmed", confirmed_at=now,
                ))
            row.status = "approved"
            row.approved_version = expected_version
            row.updated_at = now
        conn.flush()
        result = store.public_document(row)
        result["idempotent"] = False
        return result


def list_memories(user_id: str, project_id: str) -> list[dict[str, Any]]:
    with migration_session() as conn:
        _project(conn, user_id, project_id)
        rows = conn.execute(select(store.ResearchMemory).where(
            store.ResearchMemory.user_id == user_id, store.ResearchMemory.project_id == project_id
        ).order_by(store.ResearchMemory.confirmed_at.desc())).scalars().all()
        result = []
        for row in rows:
            item = store.model_dict(row)
            item.pop("user_id", None)
            result.append(item)
        return result


def _terms(text: str) -> set[str]:
    normalized = re.sub(r"\s+", "", text.lower())
    han = "".join(re.findall(r"[\u4e00-\u9fff]", normalized))
    terms = {han[index:index + 2] for index in range(max(0, len(han) - 1))}
    terms.update(re.findall(r"[a-z0-9][a-z0-9_.-]+", normalized))
    return {term for term in terms if len(term) > 1}


def search_confirmed(
    user_id: str,
    project_id: str,
    query: str,
    *,
    cross_project_ids: list[str] | None = None,
    limit: int = 3,
) -> list[dict[str, Any]]:
    project_ids = [project_id] + [item for item in (cross_project_ids or []) if item != project_id]
    with migration_session() as conn:
        for item in project_ids:
            _project(conn, user_id, item)
        rows = conn.execute(
            select(store.ResearchMemory, store.ResearchChunk, store.ResearchDocument)
            .join(store.ResearchChunk, (store.ResearchChunk.id == store.ResearchMemory.chunk_id) &
                  (store.ResearchChunk.document_version == store.ResearchMemory.document_version))
            .join(store.ResearchDocument, store.ResearchDocument.id == store.ResearchMemory.document_id)
            .where(
                store.ResearchMemory.user_id == user_id,
                store.ResearchMemory.project_id.in_(project_ids),
                store.ResearchDocument.status == "approved",
                store.ResearchDocument.version == store.ResearchMemory.document_version,
                store.ResearchDocument.approved_version == store.ResearchMemory.document_version,
            ).order_by(store.ResearchMemory.confirmed_at.desc())
        ).all()
    query_terms = _terms(query)
    if not query_terms:
        return []
    quantity_question = bool(re.search(r"多少|几个|数量|上限", query))
    ranked: list[dict[str, Any]] = []
    for memory, chunk, document in rows:
        content_terms = _terms(chunk.content)
        matched = query_terms & content_terms
        coverage = len(matched) / len(query_terms)
        if coverage < 0.16:
            continue
        if quantity_question and not re.search(r"\d|数量|上限|最多|最少|多个", chunk.content):
            continue
        score = coverage + len(matched) / max(1, len(content_terms)) ** 0.5 * 0.05
        ranked.append({
            "memory_id": memory.id, "project_id": memory.project_id, "document_id": memory.document_id,
            "document_version": memory.document_version, "chunk_id": memory.chunk_id, "title": memory.title,
            "chapter": memory.chapter, "confidentiality": memory.confidentiality, "content": chunk.content,
            "original_filename": document.original_filename, "score": round(score, 6),
            "matched_terms": sorted(matched),
        })
    ranked.sort(key=lambda item: item["score"], reverse=True)
    return ranked[:limit]


def create_conversation(user_id: str, project_id: str, title: str = "新会话") -> dict[str, Any]:
    with memory_session() as conn:
        _project(conn, user_id, project_id)
        conversation_id = _id("cnv_")
        now = store.utc_now()
        row = store.ResearchConversation(
            id=conversation_id, user_id=user_id, project_id=project_id,
            title=title.strip()[:80] or "新会话", created_at=now,
        )
        conn.add(row)
        conn.flush()
        return store.model_dict(row)


def list_conversations(user_id: str, project_id: str) -> list[dict[str, Any]]:
    with migration_session() as conn:
        _project(conn, user_id, project_id)
        rows = conn.execute(select(store.ResearchConversation).where(
            store.ResearchConversation.user_id == user_id, store.ResearchConversation.project_id == project_id
        ).order_by(store.ResearchConversation.created_at.desc())).scalars().all()
        return [store.model_dict(row) for row in rows]


def conversation_messages(user_id: str, conversation_id: str) -> list[dict[str, Any]]:
    with migration_session() as conn:
        conversation = conn.execute(select(store.ResearchConversation).where(
            store.ResearchConversation.id == conversation_id, store.ResearchConversation.user_id == user_id
        )).scalar_one_or_none()
        if conversation is None:
            raise ResearchError(404, "conversation_not_found", "会话不存在")
        rows = conn.execute(select(store.ResearchMessage).where(
            store.ResearchMessage.conversation_id == conversation_id,
            store.ResearchMessage.user_id == user_id,
        ).order_by(store.ResearchMessage.id)).scalars().all()
        result = []
        for row in rows:
            item = store.model_dict(row)
            item["sources"] = store.json_value(item.pop("sources_json"), [])
            item.pop("user_id", None)
            result.append(item)
        return result


def _save_message(conn, conversation_id: str, user_id: str, project_id: str, role: str, content: str,
                  confidentiality: str, sources: list[dict[str, Any]], model: str) -> None:
    conn.add(store.ResearchMessage(
        conversation_id=conversation_id, user_id=user_id, project_id=project_id, role=role, content=content,
        confidentiality=confidentiality, sources_json=json.dumps(sources, ensure_ascii=False),
        model=model, created_at=store.utc_now(),
    ))


def _sources(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{
        "document_id": item["document_id"], "version": item["document_version"], "chunk_id": item["chunk_id"],
        "title": item["title"], "chapter": item["chapter"], "quote": item["content"],
        "confidentiality": item["confidentiality"], "project_id": item["project_id"],
    } for item in matches]


def _model_destination(model_id: str) -> tuple[str, str]:
    from app.gateway.config import get_config

    spec = get_config().models.get(model_id)
    if spec is None:
        raise ResearchError(400, "unknown_external_model", "请选择已配置的外部模型，系统不会自动切换")
    return spec.vendor, spec.base_url


def _outbound_fragment(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "document_id": item["document_id"], "version": item["document_version"], "chunk_id": item["chunk_id"],
        "chapter": item["chapter"], "snippet": item["content"], "classification": item["confidentiality"],
        "sha256": hashlib.sha256(item["content"].encode("utf-8")).hexdigest(),
    }


def prepare_documents_outbound(
    user_id: str,
    project_id: str,
    *,
    task_id: str,
    document_ids: list[str],
    provider: str,
    model_id: str,
    purpose: str,
    operation: str,
) -> dict[str, Any]:
    actual_provider, destination = _model_destination(model_id)
    if provider != actual_provider:
        raise ResearchError(400, "provider_mismatch", "服务商与所选模型不一致")
    fragments: list[dict[str, Any]] = []
    with migration_session() as conn:
        _project(conn, user_id, project_id)
        for document_id in document_ids:
            row = _document(conn, user_id, document_id)
            if row.project_id != project_id:
                raise ResearchError(400, "document_project_mismatch", "资料不属于所选课题")
            chunks = conn.execute(select(store.ResearchChunk).where(
                store.ResearchChunk.document_id == document_id,
                store.ResearchChunk.document_version == row.version,
            ).order_by(store.ResearchChunk.ordinal)).scalars().all()
            fragments.extend(_outbound_fragment({
                "document_id": chunk.document_id, "document_version": chunk.document_version,
                "chunk_id": chunk.id, "chapter": chunk.chapter, "content": chunk.content,
                "confidentiality": chunk.confidentiality,
            }) for chunk in chunks)
    return gateway.prepare(
        user_id, project_id, task_id=task_id, provider=actual_provider, model_id=model_id, destination=destination,
        purpose=purpose, operation=operation, fragments=fragments,
    )


def chat(
    user_id: str,
    project_id: str,
    *,
    message: str,
    conversation_id: str | None = None,
    cross_project_ids: list[str] | None = None,
    provider: str = "local",
    model_id: str = "",
    query_confidentiality: str = "L1",
    task_id: str = "",
    approval_id: str = "",
) -> dict[str, Any]:
    message = message.strip()
    if not message or len(message) > 20000:
        raise ResearchError(400, "invalid_message", "问题须为 1–20000 个字符")
    if query_confidentiality not in gateway.LEVELS:
        raise ResearchError(400, "invalid_confidentiality", "检索词等级必须为 L1、L2 或 L3")
    with memory_session() as conn:
        _project(conn, user_id, project_id)
        if conversation_id:
            conversation = conn.execute(select(store.ResearchConversation).where(
                store.ResearchConversation.id == conversation_id,
                store.ResearchConversation.user_id == user_id,
                store.ResearchConversation.project_id == project_id,
            )).scalar_one_or_none()
            if conversation is None:
                raise ResearchError(404, "conversation_not_found", "会话不存在或不属于当前课题")
        else:
            conversation_id = _id("cnv_")
            conn.add(store.ResearchConversation(
                id=conversation_id, user_id=user_id, project_id=project_id,
                title=message[:40], created_at=store.utc_now(),
            ))
        _save_message(conn, conversation_id, user_id, project_id, "user", message, query_confidentiality, [], "user")
    matches = search_confirmed(user_id, project_id, message, cross_project_ids=cross_project_ids)
    sources = _sources(matches)
    classification = gateway.highest_level([query_confidentiality] + [item["confidentiality"] for item in matches])
    if not matches:
        answer = "未找到可核验证据。所选课题范围中没有与该问题匹配的已确认资料；系统不会用未审核内容或其他课题资料补答。\n\n【请复核，存在幻觉风险】"
        model = "local-no-evidence-v1"
    elif provider == "local":
        top = matches[0]
        sources = _sources(matches[:1])
        answer = (
            f"根据已确认资料《{top['title']}》“{top['chapter']}”：\n\n{top['content']}\n\n"
            "以上为来源原文摘录，不是新增实验结论。\n\n【请复核，存在幻觉风险】"
        )
        model = "local-extractive-v1"
    elif provider == "external":
        actual_provider, destination = _model_destination(model_id)
        fragments = [_outbound_fragment(item) for item in matches]
        fragments.insert(0, {
            "document_id": None, "version": 0, "chunk_id": "query", "chapter": "用户问题",
            "snippet": message, "classification": query_confidentiality,
            "sha256": hashlib.sha256(message.encode("utf-8")).hexdigest(),
        })
        task = task_id or _id("task_")
        if not approval_id:
            approval = gateway.prepare(
                user_id, project_id, task_id=task, provider=actual_provider, model_id=model_id,
                destination=destination, purpose="科研课题问答", operation="qa", fragments=fragments,
                conversation_id=conversation_id,
            )
            if approval["status"] != "approved":
                return {"status": "awaiting_outbound_approval", "conversation_id": conversation_id,
                        "task_id": task, "approval": approval, "sources": sources}
            approval_id = approval["id"]
        else:
            approval = gateway.get(user_id, approval_id)
            expected_hash = gateway.scope_hash(actual_provider, destination, "科研课题问答", "qa", fragments)
            current = gateway.prepare(
                user_id, project_id, task_id=approval["task_id"], provider=actual_provider, model_id=model_id,
                destination=destination, purpose="科研课题问答", operation="qa", fragments=fragments,
                conversation_id=conversation_id,
            )
            if current["id"] != approval_id or current["status"] != "approved":
                raise ResearchError(409, "approval_scope_changed", "材料、版本、服务商或用途已变化，请重新审批",
                                    approval=current)
            actual = gateway.scope_hash(current["provider"], current["destination"], current["purpose"],
                                        current["operation"], current["scope"]["fragments"])
            if actual != expected_hash:
                raise ResearchError(409, "approval_scope_changed", "审批范围校验失败", approval=current)
        answer = gateway.execute_once(user_id, approval_id, gateway.configured_model_sender)
        answer += "\n\n【请复核，存在幻觉风险；外部模型输出未经事实核验】"
        model = f"external:{model_id}"
    else:
        raise ResearchError(400, "invalid_provider", "处理通道必须为 local 或 external")
    with memory_session() as conn:
        _save_message(conn, conversation_id, user_id, project_id, "assistant", answer, classification, sources, model)
    return {
        "status": "answered", "conversation_id": conversation_id, "answer": answer,
        "sources": sources, "model": model, "confidentiality": classification,
        "scope_project_ids": [project_id] + [item for item in (cross_project_ids or []) if item != project_id],
    }


def security_events(user_id: str, project_id: str, limit: int = 100) -> list[dict[str, Any]]:
    with migration_session() as conn:
        _project(conn, user_id, project_id)
        rows = conn.execute(select(store.ResearchSecurityEvent).where(
            store.ResearchSecurityEvent.user_id == user_id,
            store.ResearchSecurityEvent.project_id == project_id,
        ).order_by(store.ResearchSecurityEvent.id.desc()).limit(limit)).scalars().all()
        result = []
        for row in rows:
            item = store.model_dict(row)
            item.pop("user_id", None)
            result.append(item)
        return result
