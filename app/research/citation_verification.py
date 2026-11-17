from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any

from app.research import gateway


def _normalized(value: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", value).casefold()


def _record(message: dict[str, Any]) -> dict[str, Any]:
    issued = (message.get("issued") or {}).get("date-parts") or [[]]
    year = issued[0][0] if issued and issued[0] else None
    authors = message.get("author") or []
    return {
        "doi": message.get("DOI") or "",
        "title": (message.get("title") or [""])[0],
        "first_author": (authors[0] if authors else {}),
        "year": year,
        "url": message.get("URL") or "",
    }


def _checks(record: dict[str, Any], title: str, author: str, year: int | None) -> list[dict[str, Any]]:
    checks = []
    if title:
        checks.append({"field": "title", "expected": title, "actual": record["title"], "match": _normalized(title) == _normalized(record["title"])})
    if author:
        actual_author = str(record["first_author"].get("family") or record["first_author"].get("name") or "")
        checks.append({"field": "first_author", "expected": author, "actual": actual_author, "match": _normalized(author) in _normalized(actual_author)})
    if year is not None:
        checks.append({"field": "year", "expected": year, "actual": record["year"], "match": year == record["year"]})
    return checks


def verify_citation(
    user_id: str,
    project_id: str,
    *,
    doi: str = "",
    title: str = "",
    author: str = "",
    year: int | None = None,
    confidentiality: str = "L1",
    task_id: str = "",
    approval_id: str = "",
) -> dict[str, Any]:
    from app.research.service import ResearchError

    doi, title, author = doi.strip(), title.strip(), author.strip()
    if not doi and not title:
        raise ResearchError(400, "citation_lookup_required", "请提供 DOI 或论文标题")
    if len(doi) > 200 or len(title) > 500 or len(author) > 200:
        raise ResearchError(400, "citation_lookup_too_long", "引用元数据长度超过限制")
    if confidentiality == "L3":
        raise gateway.PolicyError(403, "l3_external_forbidden", "L3 引用元数据不能连接在线服务；请使用本地资料人工核验")
    if confidentiality not in ("L1", "L2"):
        raise ResearchError(400, "invalid_confidentiality", "引用核验等级必须为 L1 或 L2")
    metadata = json.dumps({"doi": doi, "title": title, "author": author, "year": year}, ensure_ascii=False, sort_keys=True)
    task = task_id or "task_" + uuid.uuid4().hex
    approval = gateway.prepare(
        user_id, project_id, task_id=task, provider="crossref", model_id="works",
        destination="https://api.crossref.org", purpose="学术引用元数据核验", operation="search",
        fragments=[{
            "document_id": None, "version": 0, "chunk_id": "citation", "chapter": "引用元数据",
            "snippet": metadata, "classification": confidentiality,
            "sha256": hashlib.sha256(metadata.encode("utf-8")).hexdigest(),
        }],
    )
    if approval["status"] == "pending":
        return {"status": "awaiting_outbound_approval", "approval": approval, "task_id": task}
    if approval_id and approval["id"] != approval_id:
        raise ResearchError(409, "approval_scope_changed", "引用元数据已变化，请重新审批")

    def sender(_approval):
        if doi:
            url = "https://api.crossref.org/works/" + urllib.parse.quote(doi, safe="")
        else:
            url = "https://api.crossref.org/works?" + urllib.parse.urlencode({
                "query.bibliographic": title, "rows": 1,
            })
        if os.getenv("CROSSREF_MAILTO"):
            url += ("&" if "?" in url else "?") + "mailto=" + urllib.parse.quote(os.environ["CROSSREF_MAILTO"])
        request = urllib.request.Request(url, headers={"User-Agent": "ResearchStarship/1.0"})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return json.dumps({"status": "not-found", "record": None, "checks": [], "claim_verification": "manual_required"})
            raise
        message = (payload.get("message") or {})
        if doi and not message:
            return json.dumps({"status": "not-found", "record": None, "checks": [], "claim_verification": "manual_required"})
        if not doi:
            items = message.get("items") or []
            message = items[0] if items else {}
            if not message:
                return json.dumps({"status": "not-found", "record": None, "checks": [], "claim_verification": "manual_required"})
        record = _record(message)
        checks = _checks(record, title, author, year)
        status = "verified" if doi and all(item["match"] for item in checks) else "mismatch" if doi else "uncertain"
        return json.dumps({"status": status, "record": record, "checks": checks, "claim_verification": "manual_required"}, ensure_ascii=False)

    result = json.loads(gateway.execute_once(user_id, approval["id"], sender))
    return {**result, "task_id": task, "source": "crossref"}
