from __future__ import annotations

import hashlib
import json
import math
import uuid
from typing import Any

from sqlalchemy import select

from app.data_access.research import memory_session, migration_session
from app.research import gateway, store

FIELDS = ("method", "baseline", "datasets", "metrics", "limitations")
FIELD_LABELS = {
    "method": "方法", "baseline": "基线", "datasets": "数据集", "metrics": "指标", "limitations": "局限",
}
MAX_TOTAL_CHARACTERS = 30000
MAX_DOCUMENT_CHARACTERS = 6000
MAX_FACT_CHARACTERS = 1600
MAX_FACTS_PER_DOCUMENT = 12
MAX_QUERY_CHARACTERS = 2000


def _id(prefix: str) -> str:
    return prefix + uuid.uuid4().hex


def _conditions(fact: dict[str, Any]) -> str:
    comparison = fact.get("comparison") or {}
    labels = {
        "dataset": "数据集", "split": "划分", "metric": "指标口径", "unit": "单位",
        "settings": "实验设置", "model_version": "模型版本",
    }
    return "；".join(f"{labels[key]}：{comparison[key]}" for key in labels if comparison.get(key))


def _bounded_fact(fact: dict[str, Any]) -> dict[str, Any]:
    bounded = dict(fact)
    bounded["statement"] = str(fact.get("statement") or "").strip()[:600]
    bounded["conditions"] = _conditions(fact)[:300]
    value = "" if fact.get("value") is None else f"（{fact['value']}{fact.get('unit') or ''}）"
    prefix = f"{fact['field_label']}：{bounded['statement']}{value}"
    if bounded["conditions"]:
        prefix += f"；{bounded['conditions']}"
    prefix += "；原文："
    quote = str(fact.get("quote") or "")
    bounded["quote"] = quote[:max(1, MAX_FACT_CHARACTERS - len(prefix))]
    bounded["source_truncated"] = (
        len(str(fact.get("statement") or "").strip()) > 600
        or len(_conditions(fact)) > 300
        or len(quote) > len(bounded["quote"])
    )
    return bounded


def _fact_size(fact: dict[str, Any]) -> int:
    value = "" if fact.get("value") is None else f"（{fact['value']}{fact.get('unit') or ''}）"
    conditions = f"；{fact['conditions']}" if fact.get("conditions") else ""
    return len(f"{fact['field_label']}：{fact['statement']}{value}{conditions}；原文：{fact['quote']}")


def _round_robin(facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for fact in facts:
        key = fact["field"] if fact["field"] != "source_excerpt" else f"{fact['field']}:{fact['chapter']}"
        groups.setdefault(key, []).append(fact)
    ordered = []
    while any(groups.values()):
        for key in list(groups):
            if groups[key]:
                ordered.append(groups[key].pop(0))
    return ordered


def _comparison_row(conn, user_id: str, project_id: str, document_id: str) -> dict[str, Any]:
    from app.research.service import ResearchError, _document

    row = _document(conn, user_id, document_id)
    if row.project_id != project_id or row.status != "approved" or row.approved_version != row.version:
        raise ResearchError(409, "synthesis_source_unavailable", "跨文献综合只使用当前课题中已确认的有效资料")
    chunks = conn.scalars(select(store.ResearchChunk).where(
        store.ResearchChunk.document_id == row.id, store.ResearchChunk.document_version == row.version,
    ).order_by(store.ResearchChunk.ordinal)).all()
    by_id = {chunk.id: chunk for chunk in chunks}
    analysis = store.json_value(row.analysis_json, {})
    facts = []
    for field in FIELDS:
        for fact in analysis.get(field) or []:
            evidence = fact.get("evidence") or {}
            chunk = by_id.get(evidence.get("chunk_id"))
            quote = str(evidence.get("quote") or "")
            if chunk is None or quote not in chunk.content:
                continue
            facts.append(_bounded_fact({
                "field": field, "field_label": FIELD_LABELS[field],
                "statement": str(fact.get("statement") or ""),
                "value": fact.get("value"), "unit": fact.get("unit") or "",
                "comparison": fact.get("comparison") or {},
                "document_id": row.id, "version": row.version, "title": row.title,
                "confidentiality": row.confidentiality, "chunk_id": chunk.id, "chapter": chunk.chapter,
                "quote": quote, "locator": store.json_value(chunk.locator_json, {}),
                "chunk_sha256": hashlib.sha256(chunk.content.encode()).hexdigest(),
            }))
    if not facts:
        for chunk in chunks:
            facts.append(_bounded_fact({
                "field": "source_excerpt", "field_label": "原文片段（待归纳）",
                "statement": chunk.content[:600], "value": None, "unit": "", "comparison": {},
                "document_id": row.id, "version": row.version, "title": row.title,
                "confidentiality": row.confidentiality, "chunk_id": chunk.id, "chapter": chunk.chapter,
                "quote": chunk.content, "locator": store.json_value(chunk.locator_json, {}),
                "chunk_sha256": hashlib.sha256(chunk.content.encode()).hexdigest(),
            }))
    return {
        "document_id": row.id, "title": row.title, "version": row.version, "facts": facts,
        "available_fact_count": len(facts), "available_characters": sum(_fact_size(fact) for fact in facts),
    }


def _budget_rows(rows: list[dict[str, Any]]) -> None:
    selected: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        facts = []
        used = 0
        for fact in _round_robin(row["facts"]):
            size = _fact_size(fact)
            if len(facts) < MAX_FACTS_PER_DOCUMENT and used + size <= MAX_DOCUMENT_CHARACTERS:
                facts.append(fact)
                used += size
        selected[row["document_id"]] = facts
    total = 0
    finalized: dict[str, list[dict[str, Any]]] = {row["document_id"]: [] for row in rows}
    while any(selected[row["document_id"]] for row in rows):
        for row in rows:
            pending = selected[row["document_id"]]
            if not pending:
                continue
            fact = pending.pop(0)
            size = _fact_size(fact)
            if total + size <= MAX_TOTAL_CHARACTERS:
                finalized[row["document_id"]].append(fact)
                total += size
    for row in rows:
        row["facts"] = finalized[row["document_id"]]
        row["evidence"] = {
            "available_facts": row.pop("available_fact_count"),
            "selected_facts": len(row["facts"]),
            "available_characters": row.pop("available_characters"),
            "selected_characters": sum(_fact_size(fact) for fact in row["facts"]),
            "chapters": list(dict.fromkeys(fact["chapter"] for fact in row["facts"])),
            "fields": list(dict.fromkeys(fact["field"] for fact in row["facts"])),
        }
        row["evidence"]["truncated"] = (
            row["evidence"]["selected_facts"] < row["evidence"]["available_facts"]
            or row["evidence"]["selected_characters"] < row["evidence"]["available_characters"]
            or any(fact["source_truncated"] for fact in row["facts"])
        )


def _comparability(rows: list[dict[str, Any]]) -> list[str]:
    notes = []
    datasets = {row["document_id"]: sorted({fact["statement"] for fact in row["facts"] if fact["field"] == "datasets"}) for row in rows}
    if len({tuple(values) for values in datasets.values()}) > 1:
        notes.append("资料中的数据集说明不完全一致；指标只能在条件核验后比较")
    units = {fact.get("unit") for row in rows for fact in row["facts"] if fact["field"] == "metrics" and fact.get("unit")}
    if len(units) > 1:
        notes.append("指标单位或口径不同，不能直接比较数值大小")
    if any(not any(fact["field"] == "metrics" for fact in row["facts"]) for row in rows):
        notes.append("部分资料缺少已确认指标，缺失项保持待核验")
    conditions = {
        fact.get("conditions") for row in rows for fact in row["facts"]
        if fact["field"] == "metrics" and fact.get("conditions")
    }
    if len(conditions) > 1:
        notes.append("实验设置、数据划分或模型版本不完全一致，不能直接比较数值大小")
    return notes or ["未发现明显的数据集或单位冲突；仍需用户核对实验设置和指标定义"]


def _content(rows: list[dict[str, Any]], comparability: list[str], query: str) -> str:
    lines = [f"综合目标（用户输入）：{query or '比较所选资料'}", "", "| 资料 | 字段 | 内容 | 来源 |", "|---|---|---|---|"]
    for row in rows:
        for fact in row["facts"]:
            value = fact["statement"]
            if fact.get("value") is not None:
                value += f"（{fact['value']}{fact.get('unit') or ''}）"
            if fact.get("conditions"):
                value += f"；{fact['conditions']}"
            source = f"[{row['title']}｜v{row['version']}｜{fact['chapter']}]"
            lines.append(f"| {row['title']} | {fact['field_label']} | {value} | {source} |")
    lines.extend(["", *comparability, "", "## 证据范围"])
    for row in rows:
        evidence = row["evidence"]
        note = (
            f"- {row['title']}：选取 {evidence['selected_facts']}/{evidence['available_facts']} 条，"
            f"{evidence['selected_characters']}/{evidence['available_characters']} 字符；"
            f"章节：{'、'.join(evidence['chapters']) or '无'}。"
        )
        if evidence["truncated"]:
            note += "已按字段或章节代表性及固定预算截断，未选内容未被模型读取。"
        lines.append(note)
    return "\n".join(lines)


def _sources(rows: list[dict[str, Any]], project_id: str) -> list[dict[str, Any]]:
    sources = []
    seen = set()
    for row in rows:
        for fact in row["facts"]:
            key = (fact["document_id"], fact["version"], fact["chunk_id"], fact["quote"])
            if key in seen:
                continue
            seen.add(key)
            sources.append({
                "document_id": fact["document_id"], "version": fact["version"], "chunk_id": fact["chunk_id"],
                "title": fact["title"], "chapter": fact["chapter"], "quote": fact["quote"],
                "confidentiality": fact["confidentiality"], "project_id": project_id, "locator": fact["locator"],
            })
    return sources


def _validate_sources(conn, user_id: str, project_id: str, sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from app.research.notes import validate_sources
    from app.research.service import ResearchError

    try:
        return validate_sources(conn, user_id, project_id, sources)
    except ResearchError as exc:
        raise ResearchError(409, "synthesis_source_changed", "资料已变化，请重新生成") from exc


def _save_generation(
    user_id: str, project_id: str, content: str, rows: list[dict[str, Any]], model: str,
) -> str:
    sources = _sources(rows, project_id)
    with memory_session() as conn:
        sources = _validate_sources(conn, user_id, project_id, sources)
        generation_id = _id("gen_")
        row = store.ResearchGeneration(
            id=generation_id, user_id=user_id, project_id=project_id, kind="synthesis",
            content=content, sources_json=json.dumps(sources, ensure_ascii=False), model=model,
            confidentiality=gateway.highest_level([source["confidentiality"] for source in sources]),
            status="pending", archived_document_id="", created_at=store.utc_now(),
        )
        conn.add(row)
        now = store.utc_now()
        conn.add(store.ResearchStateEntry(
            id=_id("rse_"), user_id=user_id, project_id=project_id, kind="review_notice",
            content=f"跨文献综合结果 {generation_id} 的来源发生变化时需要复核",
            definition="综合结果仅作为待确认内容，不自动进入正式科研记忆", status="active",
            source_refs_json=json.dumps(sources, ensure_ascii=False),
            related_json=json.dumps({"generation_id": generation_id}, ensure_ascii=False),
            version=1, created_at=now, updated_at=now,
        ))
        conn.flush()
        return generation_id


def create_synthesis(
    user_id: str,
    project_id: str,
    *,
    document_ids: list[str],
    query: str,
    provider: str = "structured",
    model_id: str = "",
    task_id: str = "",
    approval_id: str = "",
) -> dict[str, Any]:
    from app.research.service import ResearchError

    if len(set(document_ids)) != len(document_ids) or not 2 <= len(document_ids) <= 5:
        raise ResearchError(400, "synthesis_documents_invalid", "请选择 2–5 份不重复的资料")
    if provider not in {"structured", "external"}:
        raise ResearchError(400, "synthesis_provider_invalid", "不支持的综合方式")
    query = query.strip()[:MAX_QUERY_CHARACTERS]
    with migration_session() as conn:
        rows = [_comparison_row(conn, user_id, project_id, document_id) for document_id in document_ids]
    _budget_rows(rows)
    if any(not row["facts"] for row in rows):
        raise ResearchError(422, "synthesis_evidence_empty", "所选资料没有可用于综合的有效来源片段")
    comparability = _comparability(rows)
    content = _content(rows, comparability, query)
    evidence_budget = {
        "max_total_characters": MAX_TOTAL_CHARACTERS,
        "max_document_characters": MAX_DOCUMENT_CHARACTERS,
        "max_fact_characters": MAX_FACT_CHARACTERS,
        "max_facts_per_document": MAX_FACTS_PER_DOCUMENT,
        "selected_characters": sum(row["evidence"]["selected_characters"] for row in rows),
        "selected_facts": sum(row["evidence"]["selected_facts"] for row in rows),
        "truncated": any(row["evidence"]["truncated"] for row in rows),
    }
    result = {
        "documents": rows, "comparability": comparability, "content": content, "query": query,
        "evidence_budget": evidence_budget, "status": "structured", "experiments_executed": False,
    }
    if provider == "structured":
        result["generation_id"] = _save_generation(user_id, project_id, content, rows, "structured-v2")
        return result

    from app.gateway.config import get_config
    from app.gateway import providers

    spec = get_config().models.get(model_id)
    if spec is None or not spec.has_key():
        raise ResearchError(400, "unknown_external_model", "请选择已配置的外部模型，不会自动切换")
    fragments = [{
        "document_id": None, "version": 0, "chunk_id": "query", "chapter": "综合目标",
        "snippet": query or "比较所选资料", "classification": "L1",
        "sha256": hashlib.sha256((query or "比较所选资料").encode()).hexdigest(),
    }]
    for row in rows:
        for fact in row["facts"]:
            value = "" if fact.get("value") is None else f"（{fact['value']}{fact.get('unit') or ''}）"
            conditions = f"；{fact['conditions']}" if fact.get("conditions") else ""
            snippet = f"{fact['field_label']}：{fact['statement']}{value}{conditions}；原文：{fact['quote']}"
            fragments.append({
                "document_id": fact["document_id"], "version": fact["version"], "chunk_id": fact["chunk_id"],
                "chapter": fact["chapter"], "snippet": snippet, "classification": fact["confidentiality"],
                "sha256": fact["chunk_sha256"],
            })
    task = task_id or _id("task_")
    approval = gateway.prepare(
        user_id, project_id, task_id=task, provider=spec.vendor, model_id=spec.id,
        destination=spec.base_url, purpose="跨文献综合", operation="summary", fragments=fragments,
    )
    if approval["status"] == "pending":
        return {**result, "status": "awaiting_outbound_approval", "approval": approval, "task_id": task}
    if approval_id and approval["id"] != approval_id:
        raise ResearchError(409, "approval_scope_changed", "综合材料或版本已变化，请重新审批")

    def send(_approval):
        context = "\n\n".join(f"[{item['chapter']}｜{item['document_id']}｜v{item['version']}] {item['snippet']}" for item in fragments[1:])
        response = providers.call(
            spec, api_key=spec.api_key(),
            system="仅依据给定资料进行跨文献综合。逐项区分方法、基线、数据集、实验条件、指标和局限；条件不一致时不得直接排名或判优劣；不得编造来源、数值或实验结果。",
            user=f"{context}\n\n用户目标：{query or '比较所选资料'}", tier="fast",
            timeout=float(spec.timeout or 60), temperature=0.2, max_tokens=1200,
        )
        if not response.content.strip():
            raise ResearchError(502, "synthesis_empty", "在线综合没有返回内容，未自动重试")
        return response.content.strip()

    summary = gateway.execute_once(user_id, approval["id"], send)
    generation_id = _save_generation(
        user_id, project_id, content + "\n\n## 在线综合\n" + summary, rows, f"external:{model_id}",
    )
    result["summary"] = summary
    result["status"] = "answered"
    result["generation_id"] = generation_id
    result["estimated_tokens"] = math.ceil(sum(len(item["snippet"]) for item in fragments) / 2)
    return result


def save_synthesis_note(user_id: str, generation_id: str, *, title: str) -> dict[str, Any]:
    from app.research.generations import create_study_note
    from app.research.ingest import index_document
    from app.research.service import ResearchError

    with memory_session() as conn:
        row = conn.execute(select(store.ResearchGeneration).where(
            store.ResearchGeneration.id == generation_id, store.ResearchGeneration.user_id == user_id,
        )).scalar_one_or_none()
        if row is None:
            raise ResearchError(404, "generation_not_found", "综合结果不存在")
        if row.kind != "synthesis" or row.status != "pending":
            raise ResearchError(409, "synthesis_note_unavailable", "综合结果不存在或已保存")
        sources = _validate_sources(conn, user_id, row.project_id, store.json_value(row.sources_json, []))
        result = create_study_note(
            user_id, row.project_id, title=title.strip() or "跨文献综合笔记", content=row.content,
            document_type="research_note", confidentiality=gateway.highest_level([
                source["confidentiality"] for source in sources
            ]) if sources else "L1", note_type="ai_summary", source_refs=sources,
            model_info={"model": row.model}, conn=conn, index=False,
        )
        row.status = "archived"
        row.archived_document_id = result["id"]
        conn.flush()
    index_result = index_document(user_id, result["id"])
    result["embedding_status"] = index_result["embedding_status"]
    return result
