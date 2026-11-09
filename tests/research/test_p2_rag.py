from __future__ import annotations

from pathlib import Path

import pytest

from app.research.local_components import LocalComponentError, component_status


P0_ROOT = Path(__file__).resolve().parents[3]
TUTORIAL = (P0_ROOT / "测试样本" / "虚构Agent使用教程.md").read_bytes()


@pytest.fixture
def approved_tutorial(client, auth, tmp_path, monkeypatch):
    from app.data_access import user_scope

    monkeypatch.setenv("RESEARCH_DATA_DIR", str(tmp_path / "research"))
    uid = "p2-rag-user"
    headers = auth(uid)
    with user_scope(uid):
        project = client.post("/api/research/projects", json={"name": "L3 RAG"}, headers=headers)
        project_id = project.json()["data"]["id"]
        response = client.post(
            f"/api/research/projects/{project_id}/documents",
            files={"file": ("tutorial.md", TUTORIAL, "text/markdown")},
            data={"confidentiality": "L3", "document_type": "tutorial"}, headers=headers,
        )
        assert response.status_code == 201, response.text
        document = response.json()["data"]
        response = client.post(
            f"/api/research/documents/{document['id']}/review",
            json={"expected_version": 1, "approved": True}, headers=headers,
        )
        assert response.status_code == 200, response.text
        assert document["embedding_status"] == "ready"
        yield uid, headers, project_id


def test_l3_bge_and_local_generative_rag_with_sources(client, approved_tutorial):
    if not component_status()["generative_llm"]["available"]:
        pytest.skip("local generative model unavailable")
    _, headers, project_id = approved_tutorial
    response = client.post(
        f"/api/research/projects/{project_id}/chat",
        json={"message": "资料什么时候进入正式资料库？", "provider": "rag", "query_confidentiality": "L3"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    result = response.json()["data"]
    assert result["model"].startswith("local-rag:qwen3.5-9b")
    assert result["confidentiality"] == "L3"
    assert result["sources"][0]["chapter"] == "2. 导入与确认"
    assert result["sources"][0]["retrieval"] == "bge-m3+lexical"
    assert "确认入库" in result["answer"]
    assert "[S1]" in result["answer"]


def test_local_rag_missing_model_pauses_without_external_fallback(client, approved_tutorial, monkeypatch):
    import app.research.rag as rag

    def unavailable(_prompt, **_kwargs):
        raise LocalComponentError("generative_llm", "synthetic missing model")

    monkeypatch.setattr(rag, "generate", unavailable)
    _, headers, project_id = approved_tutorial
    response = client.post(
        f"/api/research/projects/{project_id}/chat",
        json={"message": "默认搜索哪个课题？", "provider": "rag", "query_confidentiality": "L3"},
        headers=headers,
    )
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "local_model_unavailable"
