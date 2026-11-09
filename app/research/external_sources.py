from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode, urljoin, urlsplit

import httpx

from app.crawl.http import public_url, resolve_public, send
from app.research import gateway


ARXIV_HOSTS = {"arxiv.org", "export.arxiv.org"}
GITHUB_HOSTS = {"api.github.com"}


def _fragment(text: str, level: str, chunk_id: str) -> dict[str, Any]:
    return {
        "document_id": None, "version": 0, "chunk_id": chunk_id, "chapter": "外部只读请求",
        "snippet": text, "classification": level,
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }


def _run(
    user_id: str,
    project_id: str,
    *,
    task_id: str,
    approval_id: str,
    provider: str,
    model_id: str,
    destination: str,
    purpose: str,
    operation: str,
    level: str,
    fragment: dict[str, Any],
    sender: Callable[[], Any],
) -> dict[str, Any]:
    if approval_id:
        approval = gateway.get(user_id, approval_id)
        task_id = approval["task_id"]
    approval = gateway.prepare(
        user_id, project_id, task_id=task_id or "task_" + uuid.uuid4().hex,
        provider=provider, model_id=model_id, destination=destination, purpose=purpose,
        operation=operation, fragments=[fragment],
    )
    if approval["status"] != "approved":
        return {"status": "awaiting_outbound_approval", "approval": approval}
    if approval_id and approval["id"] != approval_id:
        raise gateway.PolicyError(409, "approval_scope_changed", "外部请求范围已变化，请重新审批")
    result = gateway.execute_once(user_id, approval["id"], lambda _approval: sender())
    return {"status": "completed", "result": result, "approval": approval}


def _retry_delay(response: httpx.Response | None, retries: int) -> float:
    default = 2.0 if retries == 2 else 5.0
    if response is None:
        return default
    raw = (response.headers.get("Retry-After") or "").strip()
    try:
        delay = float(raw)
    except ValueError:
        return default
    return delay if 0 <= delay <= 30 else default


def _request(url: str, *, allowed_hosts: set[str], timeout: float = 30, retries: int = 0) -> httpx.Response:
    current = url
    for redirect in range(4):
        parsed = urlsplit(current)
        if parsed.scheme != "https" or parsed.hostname not in allowed_hosts:
            raise ValueError("外部请求目标或重定向未获允许")
        try:
            response = httpx.get(
                current, timeout=timeout, follow_redirects=False,
                headers={"User-Agent": "ResearchStarship/0.2 (read-only)", "Accept-Encoding": "identity"},
            )
        except httpx.HTTPError:
            if retries > 0:
                time.sleep(_retry_delay(None, retries))
                return _request(url, allowed_hosts=allowed_hosts, timeout=timeout, retries=retries - 1)
            raise
        if response.status_code in (301, 302, 303, 307, 308):
            current = urljoin(current, response.headers.get("location", ""))
            continue
        if len(response.content) > 100 * 1024 * 1024:
            raise ValueError("外部文件超过 100 MiB")
        if response.status_code >= 400:
            if response.status_code in (408, 429, 500, 502, 503, 504) and retries > 0:
                time.sleep(_retry_delay(response, retries))
                return _request(url, allowed_hosts=allowed_hosts, timeout=timeout, retries=retries - 1)
            raise ValueError(f"外部只读请求失败：HTTP {response.status_code}")
        return response
    raise ValueError("外部重定向次数过多")


def search_arxiv(
    user_id: str,
    project_id: str,
    *,
    query: str,
    level: str = "L1",
    task_id: str = "",
    approval_id: str = "",
) -> dict[str, Any]:
    query = query.strip()
    if not query or len(query) > 500:
        raise ValueError("arXiv 检索词须为 1–500 个字符")
    url = "https://export.arxiv.org/api/query?" + urlencode({
        "search_query": f"all:{query}", "start": 0, "max_results": 5,
    })

    def fetch() -> list[dict[str, Any]]:
        response = _request(url, allowed_hosts=ARXIV_HOSTS, retries=2 if level == "L1" else 0)
        root = ET.fromstring(response.content)
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        candidates = []
        for entry in root.findall("atom:entry", ns):
            identifier = (entry.findtext("atom:id", "", ns) or "").strip()
            match = re.search(r"(\d{4}\.\d{4,5})(?:v(\d+))?", identifier)
            if not match:
                continue
            candidates.append({
                "arxiv_id": match.group(1), "version": int(match.group(2) or 1),
                "title": re.sub(r"\s+", " ", entry.findtext("atom:title", "", ns)).strip(),
                "summary": re.sub(r"\s+", " ", entry.findtext("atom:summary", "", ns)).strip(),
                "authors": [author.findtext("atom:name", "", ns) for author in entry.findall("atom:author", ns)],
                "published": entry.findtext("atom:published", "", ns),
                "updated": entry.findtext("atom:updated", "", ns),
                "url": f"https://arxiv.org/abs/{match.group(1)}v{int(match.group(2) or 1)}",
            })
        return candidates

    result = _run(
        user_id, project_id, task_id=task_id, approval_id=approval_id,
        provider="arxiv", model_id="arxiv-api", destination="https://export.arxiv.org/api/query",
        purpose="arXiv 关键词候选检索", operation="search", level=level,
        fragment=_fragment(query, level, "arxiv-query"), sender=fetch,
    )
    if result["status"] == "completed":
        result["candidates"] = result.pop("result")
        result["requires_selection"] = True
    return result


def import_arxiv_paper(
    user_id: str,
    project_id: str,
    *,
    arxiv_id: str,
    version: int,
    level: str = "L1",
    task_id: str = "",
    approval_id: str = "",
) -> dict[str, Any]:
    if not re.fullmatch(r"\d{4}\.\d{4,5}", arxiv_id) or not 1 <= version <= 999:
        raise ValueError("无效的 arXiv ID 或版本")
    from app.research.service import import_document

    url = f"https://arxiv.org/pdf/{arxiv_id}v{version}"
    result = _run(
        user_id, project_id, task_id=task_id, approval_id=approval_id,
        provider="arxiv", model_id="arxiv-pdf", destination=url,
        purpose=f"下载用户选定的 arXiv {arxiv_id}v{version}", operation="search", level=level,
        fragment=_fragment(f"{arxiv_id}v{version}", level, "arxiv-pdf"), sender=lambda: _request(url, allowed_hosts=ARXIV_HOSTS, timeout=60).content,
    )
    if result["status"] != "completed":
        return result
    content = result.pop("result")
    if not content.startswith(b"%PDF"):
        raise ValueError("选定 arXiv 版本没有返回 PDF")
    document = import_document(
        user_id, project_id, filename=f"arxiv-{arxiv_id}v{version}.pdf", content=content,
        confidentiality=level, document_type="paper", source_uri=url,
    )
    return {"status": "pending_review", "document": document, "approval": result["approval"]}


def _github_repo(value: str) -> str:
    value = value.strip()
    match = re.fullmatch(r"(?:https://github\.com/)?([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?", value)
    if not match:
        raise ValueError("GitHub 仓库格式应为 owner/repo 或仓库 URL")
    return match.group(1)


def _github_headers() -> dict[str, str]:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "ResearchStarship/0.2"}
    token = (os.getenv("GITHUB_TOKEN") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _github_json(url: str) -> Any:
    response = httpx.get(url, headers=_github_headers(), timeout=30, follow_redirects=False)
    if response.status_code >= 400:
        raise ValueError(f"GitHub 只读请求失败：HTTP {response.status_code}")
    return response.json()


def import_github_repository(
    user_id: str,
    project_id: str,
    *,
    repository: str,
    ref: str = "",
    paths: list[str] | None = None,
    level: str = "L1",
    task_id: str = "",
    approval_id: str = "",
) -> dict[str, Any]:
    repo = _github_repo(repository)
    if ref and (ref.startswith((".", "/")) or ".." in ref or "?" in ref or "#" in ref or "\\" in ref):
        raise ValueError("GitHub 分支或提交引用不合法")
    requested_paths = [path for path in (paths or []) if path][:5]
    for path in requested_paths:
        if path.startswith(("/", "../", ".")) or "/../" in path or "\\" in path or "?" in path or "#" in path:
            raise ValueError("GitHub 文件路径不合法")
    scope_text = f"{repo}@{ref or 'default'}；README、依赖、指定文件：{requested_paths}"

    def fetch() -> dict[str, Any]:
        meta = _github_json(f"https://api.github.com/repos/{repo}")
        branch = ref or meta.get("default_branch") or "main"
        commit = _github_json(f"https://api.github.com/repos/{repo}/commits/{branch}")
        sha = commit.get("sha") or ""
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise ValueError("GitHub 没有返回固定提交 SHA")
        candidates = ["README.md", "README", "README.rst", "requirements.txt", "pyproject.toml", "package.json", "package-lock.json"]
        selected = list(dict.fromkeys([*requested_paths, *candidates]))
        files: list[dict[str, Any]] = []
        for path in selected:
            response = httpx.get(
                f"https://api.github.com/repos/{repo}/contents/{path}",
                params={"ref": sha}, headers=_github_headers(),
                timeout=30, follow_redirects=False,
            )
            if response.status_code == 404:
                continue
            if response.status_code >= 400:
                raise ValueError(f"GitHub 文件读取失败：{path} HTTP {response.status_code}")
            payload = response.json()
            if payload.get("type") != "file" or int(payload.get("size") or 0) > 2 * 1024 * 1024:
                continue
            try:
                content = base64.b64decode(payload.get("content") or "").decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                continue
            files.append({
                "path": path, "sha": payload.get("sha") or "", "text": content,
                "url": payload.get("html_url") or "",
            })
        if not files:
            raise ValueError("固定提交中没有可读取的 README、依赖或指定文件")
        return {"repository": repo, "ref": branch, "commit": sha, "files": files}

    result = _run(
        user_id, project_id, task_id=task_id, approval_id=approval_id,
        provider="github", model_id="github-contents", destination="https://api.github.com",
        purpose="GitHub 固定提交只读分析", operation="search", level=level,
        fragment=_fragment(scope_text, level, "github-repository"), sender=fetch,
    )
    if result["status"] != "completed":
        return result
    data = result.pop("result")
    from app.research.service import import_document

    parts = [f"# {data['repository']} 只读资料", f"固定提交：{data['commit']}"]
    for item in data["files"]:
        parts.append(f"## {item['path']} @ {data['commit']}\n{item['text']}")
    content = "\n\n".join(parts).encode("utf-8")
    document = import_document(
        user_id, project_id, filename=f"github-{data['repository'].replace('/', '-')}-{data['commit'][:12]}.md",
        content=content, confidentiality=level, document_type="repository",
        source_uri=f"https://github.com/{data['repository']}/tree/{data['commit']}",
    )
    from app.data_access.research import memory_session
    from sqlalchemy import select
    from app.research import store

    with memory_session() as conn:
        row = conn.execute(select(store.ResearchDocument).where(
            store.ResearchDocument.id == document["id"], store.ResearchDocument.user_id == user_id
        )).scalar_one_or_none()
        row.analysis_json = json.dumps(data, ensure_ascii=False)
        row.analysis_status = "ready"
        conn.flush()
    document["repository"] = data
    document["analysis"] = data
    document["analysis_status"] = "ready"
    return {"status": "pending_review", "document": document, "approval": result["approval"]}


def import_webpage(
    user_id: str,
    project_id: str,
    *,
    url: str,
    level: str = "L1",
    task_id: str = "",
    approval_id: str = "",
) -> dict[str, Any]:
    public_url(url)
    if urlsplit(url).scheme != "https":
        raise ValueError("网页导入仅允许 HTTPS")

    def fetch() -> bytes:
        current = url
        for _ in range(4):
            response = send(current, resolve_public(current), headers={"Accept": "text/html"})
            if response.status_code in (301, 302, 303, 307, 308):
                current = urljoin(current, response.headers.get("location", ""))
                public_url(current)
                if urlsplit(current).scheme != "https":
                    raise ValueError("网页重定向未获允许")
                continue
            if response.status_code != 200:
                raise ValueError(f"网页读取失败：HTTP {response.status_code}")
            return response.content
        raise ValueError("网页重定向次数过多")

    result = _run(
        user_id, project_id, task_id=task_id, approval_id=approval_id,
        provider="web", model_id="public-html", destination=url,
        purpose="用户指定网页只读导入", operation="search", level=level,
        fragment=_fragment(url, level, "webpage-url"), sender=fetch,
    )
    if result["status"] != "completed":
        return result
    content = result.pop("result")
    from app.research.service import import_document

    title = urlsplit(url).hostname or "webpage"
    document = import_document(
        user_id, project_id, filename=f"{title}.html", content=content,
        confidentiality=level, document_type="paper", source_uri=url,
    )
    return {"status": "pending_review", "document": document, "approval": result["approval"]}
