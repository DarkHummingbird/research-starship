from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any

from jsonschema import Draft202012Validator
from sqlalchemy import select

from app.data_access.research import memory_session, migration_session
from app.research import store
from app.research.local_components import LocalComponentError, generate


FIELDS = ("method", "baseline", "datasets", "metrics", "limitations")
FACT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["statement", "evidence"],
    "properties": {
        "statement": {"type": "string", "minLength": 1, "maxLength": 2000},
        "value": {"type": ["string", "number", "null"]},
        "unit": {"type": ["string", "null"], "maxLength": 100},
        "comparison": {
            "type": "object", "additionalProperties": False,
            "properties": {field: {"type": "string"} for field in ("dataset", "split", "metric", "unit", "settings", "model_version")},
        },
        "evidence": {
            "type": "object", "additionalProperties": False,
            "required": ["chunk_id", "quote"],
            "properties": {
                "chunk_id": {"type": "string", "minLength": 1},
                "quote": {"type": "string", "minLength": 1, "maxLength": 5000},
                "match_type": {"enum": ["exact", "normalized_text"]},
            },
        },
    },
}
PAPER_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["schema_version", "record_type", *FIELDS, "suspicious"],
    "properties": {
        "schema_version": {"const": "research-paper/v1"},
        "record_type": {"const": "paper_statement"},
        "method": {"type": "array", "items": FACT_SCHEMA},
        "baseline": {"type": "array", "items": FACT_SCHEMA},
        "datasets": {"type": "array", "items": FACT_SCHEMA},
        "metrics": {"type": "array", "items": FACT_SCHEMA},
        "limitations": {"type": "array", "items": FACT_SCHEMA},
        "suspicious": {"type": "array", "items": {"type": "string"}},
    },
}


class PaperValidationError(ValueError):
    pass


def _text_key(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text)
    normalized = re.sub(r"-\s*\n\s*", "-", normalized)
    return re.sub(r"\s+", " ", normalized).strip().casefold()


def _fact_source_issue(field: str, index: int, fact: dict[str, Any], by_id: dict[str, Any]) -> str:
    evidence = fact["evidence"]
    chunk = by_id.get(evidence["chunk_id"])
    if chunk is None:
        return f"{field}/{index}: 来源片段 ID 不存在"
    if evidence.get("match_type") == "normalized_text":
        if _text_key(evidence["quote"]) not in _text_key(chunk["content"]):
            return f"{field}/{index}: 规范化引文仍不在来源片段中"
    elif evidence["quote"] not in chunk["content"]:
        return f"{field}/{index}: 引文不在来源片段中"
    return ""


def validate_analysis(payload: Any, chunks: list[dict[str, Any]]) -> None:
    errors = list(Draft202012Validator(PAPER_SCHEMA).iter_errors(payload))
    if errors:
        raise PaperValidationError("; ".join(f"{'/'.join(map(str, error.path))}: {error.message}" for error in errors))
    by_id = {chunk["id"]: chunk for chunk in chunks}
    issues = [
        issue for field in FIELDS for index, fact in enumerate(payload[field])
        if (issue := _fact_source_issue(field, index, fact, by_id))
    ]
    if issues:
        raise PaperValidationError("; ".join(issues))


def quarantine_invalid_facts(payload: Any, chunks: list[dict[str, Any]]) -> tuple[Any, list[str]]:
    errors = list(Draft202012Validator(PAPER_SCHEMA).iter_errors(payload))
    if errors:
        raise PaperValidationError("; ".join(f"{'/'.join(map(str, error.path))}: {error.message}" for error in errors))
    cleaned = json.loads(json.dumps(payload))
    by_id = {chunk["id"]: chunk for chunk in chunks}
    issues = []
    for field in FIELDS:
        kept = []
        for index, fact in enumerate(payload[field]):
            issue = _fact_source_issue(field, index, fact, by_id)
            if issue:
                issues.append(issue)
            else:
                kept.append(fact)
        cleaned[field] = kept
    if issues:
        cleaned["suspicious"].extend([f"已隔离：{issue}" for issue in issues])
        cleaned["suspicious"].append(f"{len(issues)} 条模型候选因来源校验失败被隔离；原始模型输出已保留，人工复核前不能入库")
    validate_analysis(cleaned, chunks)
    return cleaned, issues


def _json_output(text: str) -> Any:
    stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I)
    start, end = stripped.find("{"), stripped.rfind("}")
    if start < 0 or end <= start:
        raise PaperValidationError("模型没有返回 JSON 对象")
    try:
        return json.loads(stripped[start:end + 1])
    except ValueError as exc:
        if stripped.count("{") > stripped.count("}"):
            raise PaperValidationError("模型输出在 JSON 闭合前被截断；已保留原始输出，请重试或检查模型输出长度限制") from exc
        raise PaperValidationError("模型返回的 JSON 无法解析") from exc


def normalize_analysis(payload: Any) -> Any:
    if not isinstance(payload, dict):
        return payload
    normalized = dict(payload)
    for field in FIELDS:
        facts = normalized.get(field)
        if not isinstance(facts, list):
            continue
        normalized[field] = []
        for fact in facts:
            if not isinstance(fact, dict):
                normalized[field].append(fact)
                continue
            item = dict(fact)
            evidence = item.get("evidence")
            if isinstance(evidence, dict):
                evidence = dict(evidence)
                for key in ("value", "unit"):
                    if key in evidence and key not in item:
                        item[key] = evidence.pop(key)
                item["evidence"] = evidence
            normalized[field].append(item)
    if isinstance(normalized.get("suspicious"), str):
        normalized["suspicious"] = [normalized["suspicious"]]
    elif normalized.get("suspicious") is None:
        normalized["suspicious"] = []
    return normalized


def normalize_evidence_sources(payload: Any, chunks: list[dict[str, Any]]) -> Any:
    if not isinstance(payload, dict):
        return payload
    normalized_count = 0
    for field in FIELDS:
        facts = payload.get(field)
        if not isinstance(facts, list):
            continue
        for fact in facts:
            if not isinstance(fact, dict) or not isinstance(fact.get("evidence"), dict):
                continue
            evidence = fact["evidence"]
            chunk = next((item for item in chunks if item["id"] == evidence.get("chunk_id")), None)
            if chunk and evidence.get("quote") in chunk["content"]:
                evidence["match_type"] = "exact"
                continue
            quote = evidence.get("quote")
            if not isinstance(quote, str):
                continue
            key = _text_key(quote)
            matches = [item for item in chunks if key and key in _text_key(item["content"])]
            if len(matches) == 1:
                evidence["chunk_id"] = matches[0]["id"]
                evidence["match_type"] = "normalized_text"
                normalized_count += 1
    if normalized_count:
        suspicious = payload.setdefault("suspicious", [])
        if isinstance(suspicious, list):
            suspicious.append(f"{normalized_count} 条引文经空白、断词或连字规范化匹配，请复核")
    return payload


def analyze_document(
    user_id: str,
    document_id: str,
    *,
    expected_version: int | None = None,
    provider: str = "external",
    model_id: str = "",
    task_id: str = "",
    approval_id: str = "",
) -> dict[str, Any]:
    from app.research.service import ResearchError, _document
    from app.research.task_control import check_cancelled
    from app.research.local_mode import require_enabled

    if provider not in ("external", "rag"):
        raise ResearchError(400, "invalid_provider", "论文分析通道必须为在线或本地生成")
    if provider == "rag":
        require_enabled(user_id)
    with migration_session() as conn:
        row = _document(conn, user_id, document_id)
        if expected_version is not None and row.version != expected_version:
            raise ResearchError(409, "document_version_changed", "资料版本已变化，请新建任务")
        if row.document_type != "paper":
            raise ResearchError(400, "paper_only", "结构化论文分析仅适用于 paper 类型资料")
        if row.status == "approved":
            raise ResearchError(409, "approved_analysis_readonly", "已确认分析不能直接覆盖，请创建新版本")
        if row.processing_status != "ready":
            raise ResearchError(409, "document_processing_incomplete", "请先完成文档解析和 OCR")
        if row.confidentiality == "L3" and provider == "external":
            from app.research import gateway

            raise gateway.PolicyError(403, "l3_external_forbidden", "L3 论文不能连接在线模型；请开启本地保密模式并选择本地生成")
        chunks = [store.model_dict(chunk) for chunk in conn.execute(select(store.ResearchChunk).where(
            store.ResearchChunk.document_id == document_id,
            store.ResearchChunk.document_version == row.version,
        ).order_by(store.ResearchChunk.ordinal)).scalars().all()]
        version = row.version
    keywords = re.compile(r"method|approach|baseline|dataset|result|accuracy|limit|方法|数据集|指标|结果|局限|实验", re.I)
    selected = sorted(chunks, key=lambda chunk: (not bool(keywords.search(chunk["content"])), chunk["ordinal"]))
    selected = sorted(selected[:5], key=lambda chunk: chunk["ordinal"])
    context_parts: list[str] = []
    remaining = 6500
    for chunk in selected:
        content = chunk["content"][:max(0, remaining)]
        if not content:
            continue
        context_parts.append(f"片段 ID: {chunk['id']}\n{content}")
        remaining -= len(content)
        if remaining <= 0:
            break
    prompt = (
        "你是论文结构化抽取器。只依据给定片段，输出一个 JSON 对象，不要 Markdown 围栏或解释。"
        "字段固定为 schema_version、record_type、method、baseline、datasets、metrics、limitations、suspicious。"
        "schema_version 填 research-paper/v1，record_type 填 paper_statement。"
        "每个字段是数组且最多 2 项，每项格式为 {\"statement\":\"简洁陈述\",\"evidence\":{\"chunk_id\":\"输入片段ID\",\"quote\":\"逐字原文\"}}。"
        "metrics 可额外包含 value（字符串、数字或 null）和 unit（字符串或 null），value 不得是对象。"
        "quote 必须逐字摘自同一个输入片段，不得跨页或跨片段，不得使用省略号拼接，长度不超过 400 个字符。"
        "优先选择能直接支持 statement 的短引文；不得编造页码或来源。找不到证据的字段留空数组，suspicious 只填字符串。"
        "statement 保持简洁。所有字符串使用合法 JSON 双引号并正确转义，不要尾随逗号；先完整输出闭合的 JSON，再考虑增加条目。"
        "不得把预期实验写成已完成，不得补造指标。\n\n" + "\n\n".join(context_parts)
    )
    try:
        if provider == "rag":
            generated = generate(prompt, max_tokens=6000, timeout=240)
        else:
            from app.research.online_generation import generate_online

            fragments = [{
                "document_id": document_id, "version": version, "chunk_id": chunk["id"],
                "chapter": chunk["chapter"], "snippet": chunk["content"], "classification": row.confidentiality,
                "sha256": hashlib.sha256(chunk["content"].encode("utf-8")).hexdigest(),
            } for chunk in selected]
            online = generate_online(
                user_id, row.project_id, model_id=model_id, task_id=task_id,
                approval_id=approval_id, purpose="论文结构化分析", operation="summary",
                prompt=prompt, fragments=fragments, max_tokens=6000,
            )
            if online["status"] == "awaiting_outbound_approval":
                return {**online, "document_id": document_id, "analysis_status": "awaiting_approval"}
            generated = {"text": online["content"], "model": online["model"]}
        payload = normalize_evidence_sources(normalize_analysis(_json_output(generated["text"])), chunks)
        payload, quarantined = quarantine_invalid_facts(payload, chunks)
    except (LocalComponentError, PaperValidationError) as exc:
        with memory_session() as conn:
            row = conn.execute(select(store.ResearchDocument).where(
                store.ResearchDocument.id == document_id,
                store.ResearchDocument.user_id == user_id,
            )).scalar_one_or_none()
            row.analysis_status = "failed"
            row.analysis_raw_json = generated["text"] if "generated" in locals() else ""
            raw_output = generated["text"][:4000] if "generated" in locals() else ""
            row.processing_error = (str(exc) + ("\n原始模型输出：\n" + raw_output if raw_output else ""))[:5500]
            conn.flush()
        from app.research.service import ResearchError
        raise ResearchError(422, "paper_analysis_invalid", f"论文结构化结果未通过校验：{exc}") from exc
    check_cancelled()
    with memory_session() as conn:
        check_cancelled()
        row = conn.execute(select(store.ResearchDocument).where(
            store.ResearchDocument.id == document_id,
            store.ResearchDocument.user_id == user_id,
        )).scalar_one_or_none()
        if row.version != version or row.status == "approved":
            from app.research.service import ResearchError
            raise ResearchError(409, "document_version_changed", "分析期间资料状态已变化，请重新检查")
        row.analysis_json = json.dumps(payload, ensure_ascii=False)
        row.analysis_raw_json = generated["text"]
        row.analysis_status = "needs_review" if quarantined else "ready"
        row.extraction_model = generated["model"]
        row.processing_error = "；".join(payload["suspicious"])
        row.updated_at = store.utc_now()
        conn.flush()
        return store.public_document(row, include_text=True)


def update_analysis(user_id: str, document_id: str, *, expected_version: int, payload: Any, resolutions: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    from app.research.service import ResearchError, _document

    with memory_session() as conn:
        row = _document(conn, user_id, document_id)
        if row.version != expected_version:
            raise ResearchError(409, "document_version_changed", "资料版本已变化，请重新预览")
        if row.status == "approved":
            raise ResearchError(409, "approved_analysis_readonly", "已确认分析不能直接覆盖，请创建新版本")
        previous = store.json_value(row.analysis_json, {})
        unresolved = [item for item in previous.get("suspicious", []) if item.startswith("已隔离：")]
        decisions = resolutions or []
        if decisions and not unresolved:
            raise ResearchError(409, "analysis_review_invalid", "没有待处理的隔离候选")
        if unresolved:
            if len(decisions) != len(unresolved) or {item.get("issue") for item in decisions} != set(unresolved):
                raise ResearchError(409, "analysis_review_incomplete", "请逐项修正或舍弃所有隔离候选后再保存")
            for decision in decisions:
                issue = decision.get("issue", "")
                match = re.match(r"^已隔离：(method|baseline|datasets|metrics|limitations)/\d+:", issue)
                action = decision.get("action")
                if not match or action not in ("corrected", "discarded"):
                    raise ResearchError(422, "analysis_review_invalid", "审核决定无效")
                if action == "corrected" and decision.get("fact") not in payload.get(match.group(1), []):
                    raise ResearchError(422, "analysis_review_invalid", "修正内容不在对应字段中")
            payload = dict(payload)
            payload["suspicious"] = [item for item in payload.get("suspicious", [])
                                     if not item.startswith("已隔离：") and "条模型候选因来源校验失败被隔离" not in item]
            payload["suspicious"].extend(f"人工审核记录：{item['issue']} → {'已修正' if item['action'] == 'corrected' else '已舍弃'}" for item in decisions)
        chunks = [store.model_dict(chunk) for chunk in conn.execute(select(store.ResearchChunk).where(
            store.ResearchChunk.document_id == document_id,
            store.ResearchChunk.document_version == row.version,
        ).order_by(store.ResearchChunk.ordinal)).scalars().all()]
        try:
            validate_analysis(payload, chunks)
        except PaperValidationError as exc:
            raise ResearchError(422, "paper_analysis_invalid", f"论文结构化结果未通过校验：{exc}") from exc
        # Existing review decisions are server-owned and survive later field edits.
        records = [item for item in previous.get("suspicious", []) if item.startswith("人工审核记录：")]
        current_records = [item for item in payload["suspicious"] if item.startswith("人工审核记录：")]
        allowed_records = records + [f"人工审核记录：{item['issue']} → {'已修正' if item['action'] == 'corrected' else '已舍弃'}" for item in decisions]
        if any(item not in allowed_records for item in current_records):
            raise ResearchError(422, "analysis_review_invalid", "审核记录由系统生成，不能手工修改")
        payload = dict(payload)
        payload["suspicious"] = [item for item in payload["suspicious"] if not item.startswith("人工审核记录：")]
        payload["suspicious"].extend(dict.fromkeys(allowed_records))
        row.analysis_json = json.dumps(payload, ensure_ascii=False)
        row.analysis_status = "ready"
        row.extraction_model = "human_reviewed"
        row.status = "pending"
        row.processing_error = "；".join(payload["suspicious"])
        row.updated_at = store.utc_now()
        conn.flush()
        return store.public_document(row, include_text=True)
