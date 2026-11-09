from __future__ import annotations

import pytest

from app.research.chunking import chunk_text, parent_metadata


def test_parent_recursive_chunks_long_sections_at_paragraph_or_sentence_boundaries():
    text = "\n\n".join(f"第 {index} 段说明实验设置。" * 80 for index in range(6))
    chunks = chunk_text(text, "parent_recursive")
    assert len(chunks) > 1
    assert all(len(chunk) <= 1600 for chunk in chunks)
    assert any(chunk.endswith("。") for chunk in chunks[:-1])
    assert "第 0 段" in chunks[0]
    assert "第 5 段" in chunks[-1]


def test_fixed_strategy_remains_available_as_fallback():
    text = "A" * 10001
    chunks = chunk_text(text, "fixed")
    assert [len(chunk) for chunk in chunks[:2]] == [5000, 5000]
    assert len(chunks) == 3


def test_parent_context_and_strategy_validation():
    metadata = parent_metadata(
        parent_id="p1", parent_chapter="方法", parent_content="完整父章节",
        strategy="parent_recursive", locator={"page": 2},
    )
    assert metadata["parent_content"] == "完整父章节"
    assert metadata["page"] == 2
    recursive = parent_metadata(
        parent_id="p1", parent_chapter="方法", parent_content="完整父章节", strategy="recursive",
    )
    assert recursive["parent_content"] == ""
    with pytest.raises(ValueError):
        chunk_text("内容", "unknown")


def test_markdown_parent_recursive_preserves_source_and_parent_context():
    from app.research.documents import parse_input
    from app.research.rag import evidence_context

    body = "\n\n".join(f"第 {index} 段包含实验条件。" * 80 for index in range(5))
    content = f"# 论文\n\n## 实验方法\n\n{body}".encode("utf-8")
    parsed = parse_input(
        "paper.md", content, document_id="doc-chunk", title="论文",
        confidentiality="L3", document_type="paper", chunking_strategy="auto",
    )
    assert len(parsed.chunks) > 1
    method_chunks = [
        chunk for chunk in parsed.chunks
        if chunk["locator_json"]["parent_chapter"] == "实验方法"
    ]
    assert len(method_chunks) > 1
    for chunk in method_chunks:
        locator = chunk["locator_json"]
        assert "第 0 段" in locator["parent_content"]
        assert "第 4 段" in locator["parent_content"]
        assert locator["chunking_strategy"] == "auto"
    context = evidence_context([{
        "title": "论文", "chapter": "实验方法（片段 1）", "document_version": 1,
        "content": method_chunks[0]["content"],
        "parent_content": method_chunks[0]["locator_json"]["parent_content"],
    }])
    assert "父块补充上下文" in context
    assert "第 0 段" in context


def test_fixed_chunking_does_not_add_parent_content():
    from app.research.documents import parse_input

    parsed = parse_input(
        "paper.md", "# 论文\n\n## 方法\n\n短内容。".encode("utf-8"),
        document_id="doc-fixed", title="论文", confidentiality="L1",
        document_type="paper", chunking_strategy="fixed",
    )
    assert len(parsed.chunks) == 2
    assert parsed.chunks[0]["locator_json"]["parent_content"] == ""
    assert parsed.chunks[0]["locator_json"]["chunking_strategy"] == "fixed"


def test_parent_recursive_document_recalls_answer_with_real_api(client, auth, tmp_path, monkeypatch):
    monkeypatch.setenv("RESEARCH_DATA_DIR", str(tmp_path / "research"))
    headers = auth("p4-chunk-recall-user")
    project = client.post("/api/research/projects", json={"name": "父子切块召回"}, headers=headers)
    project_id = project.json()["data"]["id"]
    body = "\n\n".join(f"第 {index} 段说明实验配置。" * 80 for index in range(5))
    content = f"# 教程\n\n## 方法\n\n{body}\n\n## 结果\n\n最终指标为 87.4%。".encode("utf-8")
    uploaded = client.post(
        f"/api/research/projects/{project_id}/documents",
        files={"file": ("long-tutorial.md", content, "text/markdown")},
        data={"confidentiality": "L3", "document_type": "tutorial", "chunking_strategy": "auto"},
        headers=headers,
    )
    assert uploaded.status_code == 201, uploaded.text
    document = uploaded.json()["data"]
    assert len(document["sources"]) > 2
    approved = client.post(
        f"/api/research/documents/{document['id']}/review",
        json={"expected_version": 1, "approved": True}, headers=headers,
    )
    assert approved.status_code == 200, approved.text
    response = client.post(
        f"/api/research/projects/{project_id}/chat",
        json={"message": "最终指标是多少？", "provider": "local", "query_confidentiality": "L3"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    result = response.json()["data"]
    assert "87.4%" in result["answer"]
    assert result["sources"][0]["locator"]["parent_chapter"] == "结果"
    assert result["sources"][0]["locator"]["parent_id"]


def test_semantic_chunking_uses_local_similarity_and_no_external_fallback(monkeypatch):
    from app.research import local_components

    monkeypatch.setattr(local_components, "embed_texts", lambda texts: {
        "vectors": [[0.0, 1.0] if "烹饪" in text else [1.0, 0.0] for text in texts],
    })
    text = "检索方法保持不变。" * 30 + "\n检索方法仍然保持一致。" * 30 + "\n突然改讲烹饪流程。" + "\n烹饪流程仍然继续。"
    chunks = chunk_text(text, "semantic")
    assert len(chunks) == 2
    assert "检索方法" in chunks[0]
    assert "烹饪" in chunks[1]


@pytest.mark.parametrize("strategy", ["auto", "semantic"])
def test_parent_recursive_document_recalls_answer_with_real_api(client, auth, tmp_path, monkeypatch, strategy):
    monkeypatch.setenv("RESEARCH_DATA_DIR", str(tmp_path / "research"))
    headers = auth("p4-chunk-recall-user")
    project = client.post("/api/research/projects", json={"name": "父子切块召回"}, headers=headers)
    project_id = project.json()["data"]["id"]
    body = "\n\n".join(f"第 {index} 段说明实验配置。" * 80 for index in range(5))
    content = f"# 教程\n\n## 方法\n\n{body}\n\n## 结果\n\n最终指标为 87.4%。".encode("utf-8")
    uploaded = client.post(
        f"/api/research/projects/{project_id}/documents",
        files={"file": ("long-tutorial.md", content, "text/markdown")},
        data={"confidentiality": "L3", "document_type": "tutorial", "chunking_strategy": strategy},
        headers=headers,
    )
    assert uploaded.status_code == 201, uploaded.text
    document = uploaded.json()["data"]
    assert len(document["sources"]) > 2
    approved = client.post(
        f"/api/research/documents/{document['id']}/review",
        json={"expected_version": 1, "approved": True}, headers=headers,
    )
    assert approved.status_code == 200, approved.text
    response = client.post(
        f"/api/research/projects/{project_id}/chat",
        json={"message": "最终指标是多少？", "provider": "local", "query_confidentiality": "L3"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    result = response.json()["data"]
    assert "87.4%" in result["answer"]
    assert result["sources"][0]["locator"]["parent_chapter"] == "结果"
    assert result["sources"][0]["locator"]["parent_id"]
    assert result["sources"][0]["locator"]["chunking_strategy"] == strategy
