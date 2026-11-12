from __future__ import annotations

import hashlib
import json
import math
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
                store.ResearchDocument.project_id == row.id, store.ResearchDocument.status != "deleted"
            ))
            item["pending_count"] = conn.scalar(select(func.count(store.ResearchDocument.id)).where(
                store.ResearchDocument.project_id == row.id, store.ResearchDocument.status == "pending"
            ))
            item["memory_count"] = conn.scalar(select(func.count(store.ResearchMemory.id)).where(
                store.ResearchMemory.project_id == row.id, store.ResearchMemory.active.is_(True)
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
    return import_document(
        user_id, project_id, filename=filename, content=content,
        confidentiality=confidentiality, document_type="tutorial",
    )


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
    from app.research.ingest import import_document as ingest

    return ingest(
        user_id, project_id, filename=filename, content=content,
        confidentiality=confidentiality, document_type=document_type, source_uri=source_uri,
        chunking_strategy=chunking_strategy,
    )


def list_documents(user_id: str, project_id: str) -> list[dict[str, Any]]:
    with migration_session() as conn:
        _project(conn, user_id, project_id)
        rows = conn.execute(select(store.ResearchDocument).where(
            store.ResearchDocument.user_id == user_id, store.ResearchDocument.project_id == project_id,
            store.ResearchDocument.status != "deleted",
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
            "locator": store.json_value(source.locator_json, {}),
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
        from app.research.lifecycle import document_state, snapshot

        before = document_state(row)
        snapshot(conn, row, "before_update")
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
                locator_json=chunk.get("locator_json") or "{}",
            ))
        from app.research.notes import mark_source_invalid
        mark_source_invalid(conn, document_id, reason="来源资料已更新，关联笔记需要重新核对")
        conn.flush()
        after = document_state(row)
        from app.research.lifecycle import record_change

        result = {**after, "change": record_change(conn, row, operation="update", before=before, after=after)}
    from app.research.ingest import index_document

    result["embedding_status"] = index_document(user_id, document_id)["embedding_status"]
    return result


def review_document(user_id: str, document_id: str, *, expected_version: int, approved: bool) -> dict[str, Any]:
    with memory_session(commit_on=(ResearchError,)) as conn:
        row = _document(conn, user_id, document_id)
        if row.version != expected_version:
            raise ResearchError(409, "document_version_changed", "资料版本已变化，原审核预览失效")
        if approved and row.processing_status != "ready":
            raise ResearchError(409, "document_processing_incomplete", "解析或 OCR 尚未完成，不能确认入库")
        if approved and row.document_type == "paper" and row.analysis_status != "ready":
            raise ResearchError(409, "paper_analysis_required", "论文结构化分析尚未通过校验，请先完成分析或人工修正")
        if approved and row.status == "approved" and row.approved_version == expected_version:
            result = store.public_document(row)
            result["idempotent"] = True
            return result
        if not approved and row.status == "rejected":
            result = store.public_document(row)
            result["idempotent"] = True
            return result
        from app.research.lifecycle import document_state, snapshot

        before = document_state(row)
        snapshot(conn, row, "before_review")
        now = store.utc_now()
        if not approved:
            conn.execute(delete(store.ResearchMemory).where(store.ResearchMemory.document_id == document_id))
            row.status = "rejected"
            row.approved_version = None
            row.updated_at = now
            from app.research.notes import mark_source_invalid
            mark_source_invalid(conn, document_id, reason="来源资料已取消确认，关联笔记需要重新核对")
        else:
            if row.document_type == "research_note":
                from app.research.notes import refresh_for_review
                refresh_for_review(conn, user_id, row)
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
            from app.research.conflicts import check_document_conflicts

            check_document_conflicts(conn, user_id, row)
            by_chunk = {chunk.id: chunk for chunk in chunks}
            conn.execute(delete(store.ResearchMemory).where(
                store.ResearchMemory.document_id == document_id,
                store.ResearchMemory.document_version == expected_version,
            ))
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
            if row.document_type == "paper":
                from app.research.paper_analysis import FIELDS, validate_analysis

                analysis = store.json_value(row.analysis_json, {})
                try:
                    validate_analysis(analysis, chunk_dicts)
                except ValueError as exc:
                    raise ResearchError(422, "invalid_extraction", f"论文分析或来源校验失败：{exc}") from exc
                for field in FIELDS:
                    for index, fact in enumerate(analysis[field], 1):
                        chunk = by_chunk[fact["evidence"]["chunk_id"]]
                        statement = fact["statement"]
                        if fact.get("value") is not None:
                            statement += f"（{fact['value']}{fact.get('unit') or ''}）"
                        conn.add(store.ResearchMemory(
                            id=_id("mem_"), user_id=user_id, project_id=row.project_id, document_id=document_id,
                            document_version=expected_version, chunk_id=chunk.id, entry_id=f"{field}-{index}",
                            title=parsed["title"], statement=statement, chapter=chunk.chapter,
                            record_type="paper_statement", confidentiality=row.confidentiality,
                            review_status="confirmed", confirmed_at=now, fact_json=json.dumps(fact, ensure_ascii=False),
                        ))
                from app.research.conflicts import apply_conflict_resolutions

                apply_conflict_resolutions(conn, user_id, document_id)
            row.status = "approved"
            row.approved_version = expected_version
            row.updated_at = now
        conn.flush()
        after = document_state(row)
        from app.research.lifecycle import record_change

        result = {**after, "change": record_change(
            conn, row, operation="approve" if approved else "reject", before=before, after=after,
        )}
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


def _term_tokens(text: str) -> list[str]:
    normalized = text.lower()
    han = "".join(re.findall(r"[\u4e00-\u9fff]", normalized))
    tokens = [han[index:index + 2] for index in range(max(0, len(han) - 1))]
    tokens.extend(re.findall(r"[a-z0-9][a-z0-9_.-]+", normalized))
    return [term for term in tokens if len(term) > 1]


def _terms(text: str) -> set[str]:
    return set(_term_tokens(text))


def _term_counts(text: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for term in _term_tokens(text):
        counts[term] = counts.get(term, 0) + 1
    return counts


def _bm25_context(documents_terms: list[dict[str, int]]) -> tuple[dict[str, int], float]:
    document_frequency: dict[str, int] = {}
    for terms in documents_terms:
        for term in terms:
            document_frequency[term] = document_frequency.get(term, 0) + 1
    average_length = sum((sum(terms.values()) for terms in documents_terms), 0) / max(1, len(documents_terms))
    return document_frequency, average_length


def _bm25_score(query_terms: set[str], content_terms: dict[str, int], document_frequency: dict[str, int], average_length: float, document_count: int) -> float:
    if not query_terms or not content_terms or not document_count:
        return 0.0
    k1, b = 1.5, 0.75
    document_length = sum(content_terms.values())
    score = 0.0
    for term in query_terms & content_terms.keys():
        frequency = document_frequency.get(term, 0)
        if not frequency:
            continue
        inverse_document_frequency = math.log1p((document_count - frequency + 0.5) / (frequency + 0.5))
        term_frequency = content_terms[term]
        score += inverse_document_frequency * (term_frequency * (k1 + 1)) / (term_frequency + k1 * (1 - b + b * document_length / average_length))
    return score


def search_confirmed(
    user_id: str,
    project_id: str,
    query: str,
    *,
    cross_project_ids: list[str] | None = None,
    limit: int = 3,
    query_confidentiality: str = "L3",
) -> list[dict[str, Any]]:
    from app.gateway import providers
    from app.research.local_components import LocalComponentError, component_status, embed_texts
    from app.research.online import embedding_spec, embed_texts as embed_online

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
                store.ResearchMemory.active.is_(True),
                store.ResearchDocument.status == "approved",
                store.ResearchDocument.version == store.ResearchMemory.document_version,
                store.ResearchDocument.approved_version == store.ResearchMemory.document_version,
            ).order_by(store.ResearchMemory.confirmed_at.desc())
        ).all()
        chunk_ids = [chunk.id for _, chunk, _ in rows]
        embeddings = conn.execute(select(store.ResearchEmbedding).where(
            store.ResearchEmbedding.user_id == user_id,
            store.ResearchEmbedding.chunk_id.in_(chunk_ids),
        )).scalars().all() if chunk_ids else []
    query_terms = _terms(query)
    if not query_terms:
        return []
    online_spec = embedding_spec()
    online_allowed = online_spec.has_key() and query_confidentiality == "L1" and all(
        document.confidentiality == "L1" for _, _, document in rows
    )
    active_model = online_spec.model if online_allowed else component_status()["embedding"].get("model", "BAAI/bge-m3:onnx")
    vector_by_chunk = {
        row.chunk_id: store.json_value(row.vector_json, []) for row in embeddings if row.model == active_model
    }
    query_vector: list[float] = []
    retrieval = "local-lexical-v1"
    if vector_by_chunk and online_allowed:
        try:
            query_vector = embed_online(online_spec, [query])["vectors"][0]
            retrieval = f"{online_spec.model}+lexical"
        except providers.ProviderError as exc:
            raise ResearchError(
                503, "external_call_failed_no_retry",
                "在线检索向量服务未完成，本次没有自动重试；请稍后手动重试",
            ) from exc
    elif vector_by_chunk and component_status()["embedding"]["available"]:
        try:
            query_vector = embed_texts([query])["vectors"][0]
            retrieval = "bge-m3+lexical"
        except LocalComponentError:
            query_vector = []
    quantity_question = bool(re.search(r"多少|几个|数量|上限", query))
    documents_terms = [_term_counts(chunk.content) for _, chunk, _ in rows]
    document_frequency, average_length = _bm25_context(documents_terms)
    bm25_by_chunk: dict[str, float] = {}
    for (_, chunk, _), content_counts in zip(rows, documents_terms, strict=True):
        bm25_by_chunk[chunk.id] = _bm25_score(query_terms, content_counts, document_frequency, average_length, len(rows))
    max_bm25 = max(bm25_by_chunk.values(), default=0.0)
    ranked: list[dict[str, Any]] = []
    for (memory, chunk, document), content_counts in zip(rows, documents_terms, strict=True):
        content_terms = set(content_counts)
        matched = query_terms & content_terms
        coverage = len(matched) / len(query_terms)
        vector = vector_by_chunk.get(chunk.id) if query_vector else []
        semantic = sum(q * float(v) for q, v in zip(query_vector, vector, strict=False)) if vector else 0.0
        if not vector and bm25_by_chunk[chunk.id] <= 0:
            continue
        if vector and coverage < 0.16 and semantic < 0.25:
            continue
        if quantity_question and not re.search(r"\d|数量|上限|最多|最少|多个|无限", chunk.content, re.I):
            continue
        if quantity_question and "课题" in query and not re.search(
            r"(?:\d+|多个|无限|无上限)[^。\n]{0,20}课题|课题[^。\n]{0,20}(?:\d+|多个|无限|无上限|上限)", chunk.content
        ):
            continue
        lexical_score = bm25_by_chunk[chunk.id]
        score = semantic * 0.95 + (lexical_score / max_bm25 if max_bm25 else 0.0) * 0.05 if vector else lexical_score
        locator = store.json_value(chunk.locator_json, {})
        ranked.append({
            "memory_id": memory.id, "project_id": memory.project_id, "document_id": memory.document_id,
            "document_version": memory.document_version, "chunk_id": memory.chunk_id, "title": memory.title,
            "chapter": memory.chapter, "confidentiality": memory.confidentiality, "content": chunk.content,
            "original_filename": document.original_filename, "score": round(score, 6),
            "matched_terms": sorted(matched), "retrieval": retrieval if vector else "local-lexical-v1",
            "semantic_score": round(semantic, 6) if vector else None,
            "parent_id": locator.get("parent_id", ""),
            "parent_content": locator.get("parent_content", ""),
            "locator": locator,
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
        "locator": item.get("locator") or {}, "retrieval": item.get("retrieval") or "local-lexical-v1",
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


def _rerank_matches(
    user_id: str,
    project_id: str,
    message: str,
    matches: list[dict[str, Any]],
    query_confidentiality: str,
    task_id: str,
    approval_id: str,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    if not matches or gateway.highest_level([query_confidentiality] + [item["confidentiality"] for item in matches]) == "L3":
        return matches, None
    from app.research.online import rerank, rerank_spec

    spec = rerank_spec()
    if not spec.has_key():
        return matches, None
    fragments = [{
        "document_id": None, "version": 0, "chunk_id": "query", "chapter": "用户问题",
        "snippet": message, "classification": query_confidentiality,
        "sha256": hashlib.sha256(message.encode("utf-8")).hexdigest(),
    }, *[_outbound_fragment(item) for item in matches]]
    approval = gateway.prepare(
        user_id, project_id, task_id=task_id or _id("task_"), provider=spec.vendor, model_id=spec.id,
        destination=spec.base_url, purpose="科研检索候选重排", operation="search", fragments=fragments,
    )
    if approval["status"] != "approved":
        return matches, {"status": "awaiting_outbound_approval", "approval": approval}
    if approval_id and approval["id"] != approval_id:
        raise ResearchError(409, "approval_scope_changed", "重排内容已变化，请重新审批")
    order = gateway.execute_once(
        user_id, approval["id"],
        lambda _approval: rerank(spec, message, [item["content"] for item in matches]),
    )
    return [matches[index] for index in order], None


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
    from app.research.task_control import check_cancelled
    check_cancelled()
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
        if not approval_id:
            _save_message(conn, conversation_id, user_id, project_id, "user", message, query_confidentiality, [], "user")
    matches = search_confirmed(
        user_id, project_id, message, cross_project_ids=cross_project_ids,
        query_confidentiality=query_confidentiality, limit=100,
    )
    matches, rerank_pending = _rerank_matches(
        user_id, project_id, message, matches, query_confidentiality, task_id, approval_id,
    )
    if rerank_pending:
        return {**rerank_pending, "conversation_id": conversation_id,
                "task_id": rerank_pending["approval"]["task_id"], "sources": _sources(matches)}
    if approval_id and gateway.get(user_id, approval_id)["purpose"] == "科研检索候选重排":
        approval_id = ""
    matches = matches[:3]
    check_cancelled()
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
    elif provider == "rag":
        from app.research.local_components import LocalComponentError
        from app.research.rag import answer as rag_answer

        try:
            generated = rag_answer(message, matches)
        except LocalComponentError as exc:
            raise ResearchError(503, "local_model_unavailable", f"本地 RAG 暂停：{exc.message}") from exc
        answer = generated["text"]
        model = generated["model"]
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
    check_cancelled()
    with memory_session() as conn:
        check_cancelled()
        for match in matches:
            current = _document(conn, user_id, match["document_id"])
            if (current.status != "approved" or current.version != match["document_version"]
                    or current.approved_version != match["document_version"]
                    or current.confidentiality != match["confidentiality"]):
                raise ResearchError(409, "document_version_changed", "回答期间来源已变化，请重新提问")
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
