from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
import unicodedata
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
    conn=None,
    index: bool = True,
) -> dict[str, Any]:
    from app.research.ingest import import_document as ingest

    return ingest(
        user_id, project_id, filename=filename, content=content,
        confidentiality=confidentiality, document_type=document_type, source_uri=source_uri,
        chunking_strategy=chunking_strategy, conn=conn, index=index,
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
    if confidentiality == "L3":
        from app.research.local_mode import require_l3

        require_l3(user_id)
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
            if row.document_type == "research_note" or store.json_value(row.parsed_json, {}).get("note_provenance"):
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
            from app.research.state import create_related_notices
            create_related_notices(conn, user_id, document_id)
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


GREEK_TERMS = {"α": "alpha", "β": "beta", "γ": "gamma", "δ": "delta", "ε": "epsilon", "ϵ": "epsilon",
               "λ": "lambda", "μ": "mu", "σ": "sigma", "θ": "theta"}


def _term_tokens(text: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", text).lower()
    for symbol, name in GREEK_TERMS.items():
        normalized = normalized.replace(symbol, f" {name} ")
    normalized = re.sub(r"-\n(?=[a-z])", "", normalized)
    han = "".join(re.findall(r"[\u4e00-\u9fff]", normalized))
    tokens = [han[index:index + 2] for index in range(max(0, len(han) - 1))]
    latin = re.findall(r"[a-z0-9][a-z0-9_.-]+", normalized)
    tokens.extend(latin)
    for token in latin:
        squashed = re.sub(r"[_.-]", "", token)
        if squashed != token and len(squashed) > 1:
            tokens.append(squashed)
    return [term for term in tokens if len(term) > 1]


def _provider_error_label(kind: str) -> str:
    return {"connect": "连接失败", "timeout": "超时", "rate_limited": "被限流", "server": "服务端错误"}.get(kind, "调用未完成")


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


def _entity_key(value: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", value).casefold()


def _method_alias(memory) -> str:
    if memory.record_type != "paper_statement" or not memory.entry_id.startswith(("method", "baseline")):
        return ""
    statement = memory.statement.strip()
    named = re.search(r"\bnamed\s+([A-Za-z][\w .-]{1,100})", statement)
    if named:
        return named.group(1).strip(" .-")
    alias = re.split(
        r"\s+(?:uses?|achieves?|reaches?|is|are|was|were)\b|采用|使用|基于|达到|是",
        statement, maxsplit=1,
    )[0].strip(" \t\n.-")
    if len(alias) < 2 or len(alias) > 120 or _entity_key(alias) in {
        "we", "this", "the", "a", "an", "method", "model", "retrieval", "study", "paper", "方法", "模型", "检索", "论文",
    }:
        return ""
    return alias


def _cover_comparison(ranked, rows, query: str, query_plan: dict[str, Any] | None, selected_document_ids: list[str] | None = None) -> list[dict[str, Any]]:
    unique = list({item["chunk_id"]: item for item in sorted(ranked, key=lambda item: item["score"], reverse=True)}.values())
    if not query_plan or query_plan.get("intent") != "compare":
        return unique
    aliases: dict[str, set[str]] = {}
    for memory, _, document in rows:
        alias = _method_alias(memory)
        if alias:
            aliases.setdefault(alias, set()).add(document.id)
    query_key = _entity_key(query)
    required: dict[str, str | None] = {}
    for alias, document_ids in aliases.items():
        if _entity_key(alias) in query_key:
            required[alias] = None
    for token in re.findall(r"[A-Za-z][A-Za-z0-9_.-]{1,}", query):
        token_key = _entity_key(token)
        if token_key in {"method", "model", "retrieval", "study", "paper", "accuracy", "result", "results", "compare", "versus"}:
            continue
        if any(token_key == _entity_key(alias) or token_key in _entity_key(alias) or _entity_key(alias) in token_key for alias in aliases):
            continue
        required.setdefault(token, "missing")
    for entity in query_plan.get("entities", []):
        match = next((alias for alias in aliases if _entity_key(entity) and (
            _entity_key(entity) == _entity_key(alias)
            or _entity_key(entity) in _entity_key(alias)
            or _entity_key(alias) in _entity_key(entity)
        )), None)
        if match:
            required[match] = None
        elif _entity_key(entity):
            required[entity] = "missing"
    priorities: dict[tuple[str, str], int] = {}
    for memory, _, document in rows:
        alias = _method_alias(memory)
        if alias:
            priorities[(alias, document.id)] = 0 if memory.entry_id.startswith("method") else 1
    if not selected_document_ids:
        ambiguous = []
        for alias, document_ids in aliases.items():
            candidates = [{
                "document_id": document.id, "title": document.title, "version": document.version, "method": alias,
            } for memory, _, document in rows if document.id in document_ids and memory.entry_id.startswith("method") and _method_alias(memory) == alias]
            unique_candidates = list({item["document_id"]: item for item in candidates}.values())
            if len(unique_candidates) > 1:
                ambiguous.append({"name": alias, "candidates": unique_candidates})
        generic = not required and len({document.id for memory, _, document in rows if memory.entry_id.startswith("method")}) >= 2
        if ambiguous or generic or query_plan.get("needs_clarification"):
            if ambiguous or generic:
                generic_candidates = []
                if generic and not ambiguous:
                    for memory, _, document in rows:
                        if memory.entry_id.startswith("method"):
                            generic_candidates.append({
                                "document_id": document.id, "title": document.title,
                                "version": document.version, "method": _method_alias(memory),
                            })
                query_plan["clarification"] = {
                    "kind": "comparison_documents",
                    "message": "同一方法对应多份资料，请选择要比较的版本" if ambiguous else "检测到多个可能的比较对象，请选择 2–5 份资料",
                    "entities": ambiguous or [{"name": "可选资料", "candidates": list({item["document_id"]: item for item in generic_candidates}.values())}],
                }
                query_plan["needs_clarification"] = True
                return []
    selected: list[dict[str, Any]] = []
    covered: list[str] = []
    for alias in required:
        candidates = [item for item in unique if item["document_id"] in aliases.get(alias, set())]
        match = min(
            candidates,
            key=lambda item: (
                priorities.get((alias, item["document_id"]), 1),
                0 if item["entry_id"].startswith("method") else 1,
                -item["score"],
            ),
            default=None,
        )
        if match:
            match.setdefault("comparison_entities", []).append(alias)
            selected.append(match)
            covered.append(alias)
        else:
            required[alias] = "missing"
    if re.search(r"准确率|效果|指标|accuracy|metric|score", query + " " + " ".join(query_plan.get("aspects", [])), re.I):
        for alias in covered:
            metric = min(
                (item for item in unique if item["document_id"] in aliases[alias] and item["entry_id"].startswith("metrics")),
                key=lambda item: (priorities.get((alias, item["document_id"]), 1), -item["score"]),
                default=None,
            )
            if metric and all(item["chunk_id"] != metric["chunk_id"] for item in selected):
                metric.setdefault("comparison_entities", []).append(alias)
                selected.append(metric)
    selected_ids = {item["chunk_id"] for item in selected}
    selected.extend(item for item in unique if item["chunk_id"] not in selected_ids)
    query_plan.update({
        "entities": list(required),
        "covered": covered,
        "missing": [alias for alias in required if alias not in covered],
    })
    return selected


def _unique_paper_mention(rows, query: str, query_plan: dict[str, Any]) -> str | None:
    aliases: dict[str, set[str]] = {}
    for memory, _, document in rows:
        if document.document_type != "paper" or not memory.entry_id.startswith("method"):
            continue
        alias = _method_alias(memory)
        if alias:
            aliases.setdefault(alias, set()).add(document.id)
    query_key = _entity_key(query)
    mentioned = {alias for alias in aliases if _entity_key(alias) and _entity_key(alias) in query_key}
    for entity in query_plan.get("entities", []):
        key = _entity_key(entity)
        if not key:
            continue
        for alias in aliases:
            alias_key = _entity_key(alias)
            if key == alias_key or key in alias_key or alias_key in key:
                mentioned.add(alias)
    document_ids = set().union(*(aliases[alias] for alias in mentioned)) if mentioned else set()
    if len(document_ids) == 1:
        return next(iter(document_ids))
    return None


def _request_paper_scope(rows, query: str, query_plan: dict[str, Any]) -> None:
    if query_plan.get("implicit_document_id"):
        return
    aliases: dict[str, set[str]] = {}
    documents: dict[str, dict[str, Any]] = {}
    for memory, _, document in rows:
        if document.document_type == "paper":
            documents.setdefault(document.id, {"title": document.title, "version": document.version})
        if not memory.entry_id.startswith("method"):
            continue
        alias = _method_alias(memory)
        if alias and document.document_type == "paper":
            aliases.setdefault(alias, set()).add(document.id)
    shared = {alias: ids for alias, ids in aliases.items() if len(ids) > 1}
    query_key = _entity_key(query)
    mentioned = {alias for alias in shared if _entity_key(alias) and _entity_key(alias) in query_key}
    for entity in query_plan.get("entities", []):
        key = _entity_key(entity)
        if not key:
            continue
        for alias in shared:
            alias_key = _entity_key(alias)
            if key == alias_key or key in alias_key or alias_key in key:
                mentioned.add(alias)
    if not mentioned:
        uniquely_named = any(
            len(ids) == 1 and _entity_key(alias) and _entity_key(alias) in query_key
            for alias, ids in aliases.items()
        )
        if uniquely_named or len(documents) < 2:
            return
        query_plan["clarification"] = {
            "kind": "paper_scope",
            "message": "这个课题有多篇论文，为避免混在一起引用，请先选择本次要查的论文；也可以选“不限定论文”。",
            "entities": [{
                "name": "可选论文",
                "candidates": [{
                    "document_id": document_id,
                    "title": info["title"],
                    "version": info["version"],
                    "method": "",
                } for document_id, info in sorted(documents.items())],
            }],
        }
        query_plan["needs_clarification"] = True
        return
    query_plan["clarification"] = {
        "kind": "paper_scope",
        "message": "问题中的方法出现在多篇论文中，请选择要检索的论文",
        "entities": [{
            "name": alias,
            "candidates": [{
                "document_id": document_id,
                "title": documents[document_id]["title"],
                "version": documents[document_id]["version"],
                "method": alias,
            } for document_id in sorted(shared[alias]) if document_id in documents],
        } for alias in sorted(mentioned)],
    }
    query_plan["needs_clarification"] = True


def search_confirmed(
    user_id: str,
    project_id: str,
    query: str,
    *,
    cross_project_ids: list[str] | None = None,
    limit: int = 3,
    query_confidentiality: str = "L3",
    query_plan: dict[str, Any] | None = None,
    selected_document_ids: list[str] | None = None,
    allow_all_documents: bool = False,
) -> list[dict[str, Any]]:
    from app.gateway import providers
    from app.research import local_mode
    from app.research.local_components import LocalComponentError, component_status, embed_texts
    from app.research.online import embedding_spec, embed_texts as embed_online

    project_ids = [project_id] + [item for item in (cross_project_ids or []) if item != project_id]
    selected_ids = set(selected_document_ids or [])
    if len(selected_ids) > 5:
        raise ResearchError(400, "comparison_documents_invalid", "最多选择 5 份资料")
    with migration_session() as conn:
        for item in project_ids:
            _project(conn, user_id, item)
        for document_id in selected_ids:
            selected_document = _document(conn, user_id, document_id)
            if selected_document.project_id not in project_ids:
                raise ResearchError(400, "document_project_mismatch", "所选资料不属于当前检索范围")
            if selected_document.status != "approved" or selected_document.approved_version != selected_document.version:
                raise ResearchError(409, "document_not_ready", "所选资料尚未确认或版本已变化")
        statement = (
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
        )
        if selected_ids:
            statement = statement.where(store.ResearchDocument.id.in_(selected_ids))
        rows = conn.execute(statement).all()
        if (not selected_ids and not allow_all_documents and query_plan is not None
                and query_plan.get("intent") == "single"):
            implicit = _unique_paper_mention(rows, query, query_plan)
            if implicit:
                rows = [row for row in rows if row[2].id == implicit]
                query_plan["implicit_document_id"] = implicit
        chunk_ids = [chunk.id for _, chunk, _ in rows]
        embeddings = conn.execute(select(store.ResearchEmbedding).where(
            store.ResearchEmbedding.user_id == user_id,
            store.ResearchEmbedding.chunk_id.in_(chunk_ids),
        )).scalars().all() if chunk_ids else []
    planned_entities = (query_plan or {}).get("entities", [])
    retrieval_query = query + (" " + " ".join(planned_entities) if planned_entities else "")
    query_terms = _terms(retrieval_query)
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
            if exc.kind not in {"connect", "timeout", "rate_limited", "server"}:
                raise ResearchError(
                    503, "external_call_failed_no_retry",
                    "在线检索向量服务未完成，本次没有自动重试；请稍后手动重试",
                ) from exc
            query_vector = []
            if query_plan is not None:
                query_plan["embedding_status"] = "degraded_local_lexical"
                query_plan["degradation_reasons"] = (query_plan.get("degradation_reasons") or []) + [{"stage": "embedding", "reason": exc.kind}]
                query_plan["retrieval_warning"] = f"在线查询向量{_provider_error_label(exc.kind)}；未重试，已降级为 BM25 词语检索；词面不一致的内容可能漏检"
    elif vector_by_chunk and local_mode.is_enabled(user_id) and component_status()["embedding"]["available"]:
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
            "entry_id": memory.entry_id, "record_type": memory.record_type,
            "chapter": memory.chapter, "confidentiality": memory.confidentiality, "content": chunk.content,
            "original_filename": document.original_filename, "score": round(score, 6),
            "matched_terms": sorted(matched), "retrieval": retrieval if vector else "local-lexical-v1",
            "semantic_score": round(semantic, 6) if vector else None,
            "parent_id": locator.get("parent_id", ""),
            "parent_content": locator.get("parent_content", ""),
            "locator": locator,
        })
    ranked.sort(key=lambda item: item["score"], reverse=True)
    covered = _cover_comparison(ranked, rows, query, query_plan, selected_document_ids)
    if (query_plan is not None and query_plan.get("intent") == "single"
            and not selected_document_ids and not allow_all_documents):
        _request_paper_scope(rows, query, query_plan)
    if query_plan and query_plan.get("clarification"):
        return []
    return covered[:limit]


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


SENTENCE_PATTERN = re.compile(r"[^。！？!?；;\n]+[。！？!?；;]?")


def _citation_span(content: str, query: str) -> tuple[str, int, int]:
    text = content or ""
    terms = _terms(query)
    chosen: tuple[int, int] | None = None
    chosen_key: tuple[int, int, int] | None = None
    for match in SENTENCE_PATTERN.finditer(text):
        raw = match.group(0)
        sentence = raw.strip()
        if len(sentence) < 12 or sentence.startswith("#"):
            continue
        start = match.start() + raw.index(sentence)
        end = start + len(sentence)
        overlap = len(_terms(sentence) & terms)
        key = (-overlap, len(sentence) if overlap else 0, start)
        if chosen_key is None or key < chosen_key:
            chosen_key, chosen = key, (start, end)
    if chosen is None:
        return "", 0, 0
    start, end = chosen
    for _ in range(4):
        if re.search(r"[。！？!?；;]$", text[start:end].rstrip()) or end >= len(text):
            break
        line_end = text.find("\n", end)
        if line_end < 0:
            end = len(text)
            break
        next_end = text.find("\n", line_end + 1)
        next_end = len(text) if next_end < 0 else next_end
        following = text[line_end + 1:next_end]
        if not following.strip() or following.lstrip().startswith("#"):
            break
        if re.match(r"\d+(?:\.\d+)*\s+\S", following.strip()):
            break
        end = next_end
    quote = text[start:end].rstrip()
    return quote, start, start + len(quote)


def _sources(matches: list[dict[str, Any]], query: str = "", *, cross_document: bool = False) -> list[dict[str, Any]]:
    result = []
    for item in matches:
        quote, start, end = _citation_span(item["content"], query)
        if not quote:
            quote, start, end = item["content"], 0, len(item["content"])
        source = {
            "document_id": item["document_id"], "version": item["document_version"], "chunk_id": item["chunk_id"],
            "title": item["title"], "chapter": item["chapter"], "quote": quote,
            "quote_start": start, "quote_end": end,
            "confidentiality": item["confidentiality"], "project_id": item["project_id"],
            "locator": item.get("locator") or {}, "retrieval": item.get("retrieval") or "local-lexical-v1",
        }
        if cross_document:
            source["cross_document"] = True
        result.append(source)
    return result


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
    query_plan: dict[str, Any] | None = None,
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
    try:
        order = gateway.execute_once(
            user_id, approval["id"],
            lambda _approval: rerank(spec, message, [item["content"] for item in matches]),
        )
    except gateway.PolicyError as exc:
        cause = exc.__cause__
        if getattr(cause, "kind", "") not in {"connect", "timeout", "rate_limited", "server"}:
            raise
        if query_plan is not None:
            query_plan["rerank_status"] = "degraded_local_order"
            reason = getattr(cause, "kind", "") or ""
            query_plan["degradation_reasons"] = (query_plan.get("degradation_reasons") or []) + [{"stage": "rerank", "reason": reason}]
            rerank_warning = f"在线 Rerank {_provider_error_label(reason)}；未重试，已使用原有本地排序，仍需核对来源"
            query_plan["retrieval_warning"] = "；".join(filter(None, [query_plan.get("retrieval_warning", ""), rerank_warning]))
        return matches, None
    ordered = [matches[index] for index in order]
    if query_plan and query_plan.get("intent") == "compare":
        protected = []
        covered_entities = set()
        for item in matches:
            entities = set(item.get("comparison_entities", []))
            if entities - covered_entities:
                protected.append(item)
                covered_entities.update(entities)
        protected_ids = {item["chunk_id"] for item in protected}
        ordered = protected + [item for item in ordered if item["chunk_id"] not in protected_ids]
    return ordered, None


def _weak_evidence(matches: list[dict[str, Any]], query: str) -> bool:
    if not matches:
        return False
    terms = _terms(query)
    if not terms:
        return False
    if any(set(item.get("matched_terms") or []) & terms for item in matches):
        return False
    semantic = max(float(item.get("semantic_score") or 0.0) for item in matches)
    return semantic < 0.5


def _degraded_without_overlap(query_plan: dict[str, Any], matches: list[dict[str, Any]], query: str) -> bool:
    degraded = (
        query_plan.get("embedding_status") == "degraded_local_lexical"
        or query_plan.get("rerank_status") == "degraded_local_order"
    )
    if not degraded or not matches:
        return False
    return not any(set(item.get("matched_terms") or []) & _terms(query) for item in matches)


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
    query_plan: dict[str, Any] | None = None,
    selected_document_ids: list[str] | None = None,
    allow_all_documents: bool = False,
) -> dict[str, Any]:
    from app.research.task_control import check_cancelled
    check_cancelled()
    message = message.strip()
    if not message or len(message) > 20000:
        raise ResearchError(400, "invalid_message", "问题须为 1–20000 个字符")
    if query_confidentiality not in gateway.LEVELS:
        raise ResearchError(400, "invalid_confidentiality", "检索词等级必须为 L1、L2 或 L3")
    from app.research import local_mode

    if query_confidentiality == "L3":
        local_mode.require_l3(user_id)
    if provider == "rag":
        local_mode.require_enabled(user_id)
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
    approval_purpose = gateway.get(user_id, approval_id)["purpose"] if approval_id else ""
    from app.research.query_planner import plan_query, rule_plan, validate_plan

    if query_plan is not None:
        try:
            query_plan = validate_plan(query_plan)
        except ValueError as exc:
            raise ResearchError(400, "invalid_query_plan", "查询计划格式无效，请重新提问") from exc
    elif approval_purpose in ("科研检索候选重排", "科研课题问答"):
        query_plan = rule_plan(message, status="rules_after_approval")
    else:
        query_plan = plan_query(
            user_id, project_id, message, provider=provider, model_id=model_id,
            query_confidentiality=query_confidentiality, task_id=task_id, approval_id=approval_id,
        )
        if query_plan.get("status") == "awaiting_outbound_approval":
            return {**query_plan, "conversation_id": conversation_id, "sources": []}
    if approval_purpose == "科研查询规划":
        approval_id = ""
    if selected_document_ids or allow_all_documents:
        query_plan["needs_clarification"] = False
        query_plan.pop("clarification", None)
    matches = search_confirmed(
        user_id, project_id, message, cross_project_ids=cross_project_ids,
        query_confidentiality=query_confidentiality, limit=100, query_plan=query_plan,
        selected_document_ids=selected_document_ids, allow_all_documents=allow_all_documents,
    )
    if provider == "external" and gateway.highest_level([query_confidentiality] + [item["confidentiality"] for item in matches]) == "L3":
        raise gateway.PolicyError(403, "l3_external_forbidden", "L3 内容不能连接在线模型；请开启本地保密模式并选择本地生成")
    if query_plan.get("implicit_document_id"):
        title = next((item["title"] for item in matches if item["document_id"] == query_plan["implicit_document_id"]), "")
        query_plan["scope_note"] = f"已按问题中的方法名限定到《{title}》" if title else "已按问题中的方法名限定检索范围"
    if query_plan.get("clarification"):
        return {
            "status": "needs_clarification", "conversation_id": conversation_id,
            "clarification": query_plan["clarification"], "query_plan": query_plan, "sources": [],
        }
    matches, rerank_pending = _rerank_matches(
        user_id, project_id, message, matches, query_confidentiality, task_id, approval_id, query_plan,
    )
    if rerank_pending:
        return {**rerank_pending, "conversation_id": conversation_id, "query_plan": query_plan,
                "task_id": rerank_pending["approval"]["task_id"], "sources": _sources(matches, message)}
    if approval_id and gateway.get(user_id, approval_id)["purpose"] == "科研检索候选重排":
        approval_id = ""
    if _weak_evidence(matches, message):
        query_plan["retrieval_warning"] = "；".join(filter(None, [
            query_plan.get("retrieval_warning", ""),
            "来源与问题的词面重合很少，可能并不相关；请核对来源后再采用",
        ]))
    comparison_count = sum(bool(item.get("comparison_entities")) for item in matches)
    matches = matches[:max(3, comparison_count)]
    cross_document = bool(matches) and query_plan.get("intent") != "compare" and len({
        item["document_id"] for item in matches
    }) > 1
    if cross_document:
        query_plan["document_scope"] = {
            "cross_document": True,
            "document_ids": sorted({item["document_id"] for item in matches}),
        }
        query_plan["retrieval_warning"] = "；".join(filter(None, [
            query_plan.get("retrieval_warning", ""),
            "本次来源来自多篇论文，回答中的事实请逐条核对论文归属",
        ]))
    degraded_no_answer = _degraded_without_overlap(query_plan, matches, message)
    check_cancelled()
    sources = _sources(matches, message, cross_document=cross_document)
    classification = gateway.highest_level([query_confidentiality] + [item["confidentiality"] for item in matches])
    if classification == "L3":
        local_mode.require_l3(user_id)
    if not matches:
        if selected_document_ids:
            answer = "未找到可核验证据。所选论文中没有与该问题匹配的已确认段落；系统不会用其他论文补答。\n\n【请复核，存在幻觉风险】"
        else:
            answer = "未找到可核验证据。所选课题范围中没有与该问题匹配的已确认资料；系统不会用未审核内容或其他课题资料补答。\n\n【请复核，存在幻觉风险】"
        model = "local-no-evidence-v1"
    elif degraded_no_answer:
        answer = ("在线语义检索本次不可用，只能按关键词检索，且找到的内容与问题词面没有交集，无法可靠回答。"
                  "请稍后重试；系统不会据此断言资料中没有该内容。\n\n【请复核，存在幻觉风险】")
        model = "degraded-no-answer-v1"
    elif provider == "local":
        top = matches[0]
        sources = _sources(matches[:1], message, cross_document=cross_document)
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
                        "task_id": task, "approval": approval, "sources": sources, "query_plan": query_plan}
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
    if cross_document:
        answer += "\n\n【跨文献提示】本次来源覆盖多篇论文，请按来源标题核对每条事实的归属。"
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
        "query_plan": query_plan,
        "retrieval_warning": query_plan.get("retrieval_warning", ""),
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
