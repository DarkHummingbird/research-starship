from __future__ import annotations

import base64

import pytest

from app.research import external_sources


@pytest.fixture
def source_user(client, auth, tmp_path, monkeypatch):
    from app.data_access import user_scope

    monkeypatch.setenv("RESEARCH_DATA_DIR", str(tmp_path / "research"))
    uid = "p2-source-user"
    headers = auth(uid)
    with user_scope(uid):
        project = client.post("/api/research/projects", json={"name": "外部来源"}, headers=headers)
        yield uid, headers, project.json()["data"]["id"]


ATOM = b'''<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
<entry><id>http://arxiv.org/abs/1706.03762v7</id><updated>2023-08-07T00:00:00Z</updated><published>2017-06-12T00:00:00Z</published><title>Attention Is All You Need</title><summary>Transformer study.</summary><author><name>Ashish Vaswani</name></author></entry>
<entry><id>http://arxiv.org/abs/1707.00000v2</id><updated>2017-07-01T00:00:00Z</updated><published>2017-07-01T00:00:00Z</published><title>Second Candidate</title><summary>Other paper.</summary></entry>
</feed>'''


def test_arxiv_candidates_then_selected_import_and_l2_gate(client, source_user, monkeypatch):
    _, headers, project_id = source_user
    calls = []

    class Response:
        content = ATOM

    monkeypatch.setattr(external_sources, "_request", lambda url, **kwargs: calls.append((url, kwargs)) or Response())
    response = client.post(
        f"/api/research/projects/{project_id}/sources/arxiv/search",
        json={"query": "transformer", "confidentiality": "L2"}, headers=headers,
    )
    assert response.status_code == 200
    pending = response.json()["data"]
    assert pending["status"] == "awaiting_outbound_approval"
    assert calls == []

    response = client.post(
        f"/api/research/projects/{project_id}/sources/arxiv/search",
        json={"query": "transformer", "confidentiality": "L1"}, headers=headers,
    )
    assert response.status_code == 200, response.text
    result = response.json()["data"]
    assert result["requires_selection"] is True
    assert len(result["candidates"]) == 2
    assert result["candidates"][0]["version"] == 7
    assert calls


def test_github_fixed_commit_readonly_files_and_l3_block(client, source_user, monkeypatch):
    _, headers, project_id = source_user
    sha = "a" * 40
    monkeypatch.setattr(external_sources, "_github_json", lambda url: (
        {"default_branch": "main"} if url.endswith("/repos/owner/repo") else {"sha": sha}
    ))

    class Response:
        status_code = 200

        def json(self):
            return {
                "type": "file", "size": 100,
                "content": base64.b64encode(b"print('read only')").decode(),
                "sha": "b" * 40, "html_url": "https://github.com/owner/repo/blob/file",
            }

    monkeypatch.setattr(external_sources.httpx, "get", lambda *args, **kwargs: Response())
    response = client.post(
        f"/api/research/projects/{project_id}/sources/github/import",
        json={"repository": "owner/repo", "paths": ["app/main.py"], "confidentiality": "L1"}, headers=headers,
    )
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert data["document"]["repository"]["commit"] == sha
    assert data["document"]["analysis_status"] == "ready"
    assert "app/main.py" in data["document"]["source_text"]
    assert data["document"]["repository"]["files"][0]["sha"] == "b" * 40

    response = client.post(
        f"/api/research/projects/{project_id}/sources/github/import",
        json={"repository": "owner/private", "confidentiality": "L3"}, headers=headers,
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "l3_external_forbidden"


def test_webpage_uses_ssrf_guard_and_parses_html(client, source_user, monkeypatch):
    _, headers, project_id = source_user
    monkeypatch.setattr(external_sources, "resolve_public", lambda url: "93.184.216.34")

    class Response:
        status_code = 200
        content = b"<title>Public Paper</title><p>Evidence text.</p>"
        headers = {}

    monkeypatch.setattr(external_sources, "send", lambda *args, **kwargs: Response())
    response = client.post(
        f"/api/research/projects/{project_id}/sources/webpage/import",
        json={"url": "https://example.com/paper", "confidentiality": "L1"}, headers=headers,
    )
    assert response.status_code == 201, response.text
    assert "Evidence text." in response.json()["data"]["document"]["source_text"]

    response = client.post(
        f"/api/research/projects/{project_id}/sources/webpage/import",
        json={"url": "http://127.0.0.1/private", "confidentiality": "L1"}, headers=headers,
    )
    assert response.status_code in (400, 500)
