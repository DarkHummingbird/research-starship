import json
import pytest

from tests.research.test_p2_analysis import paper


def test_quarantined_candidate_requires_explicit_resolution(client, paper, monkeypatch):
    import app.research.paper_analysis as analysis_module

    _, headers, _, doc = paper
    source = doc["sources"][0]
    raw = {"schema_version": "research-paper/v1", "record_type": "paper_statement",
           "method": [], "baseline": [], "datasets": [], "limitations": [], "suspicious": [],
           "metrics": [{"statement": "准确率", "value": "87.4", "unit": "%",
                        "evidence": {"chunk_id": source["id"], "quote": "not in source"}}]}
    monkeypatch.setattr(analysis_module, "generate", lambda *a, **k: {"text": json.dumps(raw), "model": "synthetic"})
    url = f"/api/research/documents/{doc['id']}"
    result = client.post(url + "/analysis", json={"provider": "rag"}, headers=headers)
    assert result.status_code == 200, result.text
    result = result.json()["data"]
    assert result["analysis_status"] == "needs_review"
    payload = result["analysis"]
    issue = next(item for item in payload["suspicious"] if item.startswith("已隔离："))
    blocked = client.post(url + "/review", headers=headers, json={"expected_version": doc["version"], "approved": True})
    assert blocked.status_code == 409
    blocked = client.put(url + "/analysis", headers=headers, json={"expected_version": doc["version"], "analysis": payload})
    assert blocked.status_code == 409
    fact = raw["metrics"][0]
    fact["evidence"]["quote"] = "SafeRAG reaches 87.4% accuracy on SYNTH-10."
    payload["metrics"] = [fact]
    saved = client.put(url + "/analysis", headers=headers, json={"expected_version": doc["version"], "analysis": payload,
        "resolutions": [{"issue": issue, "action": "corrected", "fact": fact}]})
    assert saved.status_code == 200, saved.text
    assert saved.json()["data"]["analysis_status"] == "ready"
    assert any("人工审核记录" in item for item in saved.json()["data"]["analysis"]["suspicious"])
    revised = saved.json()["data"]["analysis"]
    revised["suspicious"] = []
    resaved = client.put(url + "/analysis", headers=headers, json={"expected_version": doc["version"], "analysis": revised})
    assert resaved.status_code == 200, resaved.text
    assert any("人工审核记录" in item for item in resaved.json()["data"]["analysis"]["suspicious"])
    approved = client.post(url + "/review", headers=headers, json={"expected_version": doc["version"], "approved": True})
    assert approved.status_code == 200, approved.text


@pytest.mark.parametrize("real_embedding", [False, True])
def test_chinese_question_retrieves_english_accuracy(client, paper, monkeypatch, real_embedding):
    import app.research.local_components as local
    import app.research.paper_analysis as analysis_module

    _, headers, project, doc = paper
    source = doc["sources"][0]
    payload = {"schema_version": "research-paper/v1", "record_type": "paper_statement",
               "method": [], "baseline": [], "datasets": [], "limitations": [], "suspicious": [],
               "metrics": [{"statement": "准确率", "value": "87.4", "unit": "%",
                            "evidence": {"chunk_id": source["id"], "quote": "SafeRAG reaches 87.4% accuracy on SYNTH-10."}}]}
    monkeypatch.setattr(analysis_module, "generate", lambda *a, **k: {"text": json.dumps(payload), "model": "synthetic"})
    assert client.post(f"/api/research/documents/{doc['id']}/analysis", json={"provider": "rag"}, headers=headers).status_code == 200
    assert client.post(f"/api/research/documents/{doc['id']}/review", headers=headers, json={"expected_version": 1, "approved": True}).status_code == 200
    # A controlled similarity removes local-model availability from the filter regression.
    if real_embedding and not local.component_status()["embedding"]["available"]:
        pytest.skip("local embedding assets unavailable")
    if not real_embedding:
        monkeypatch.setattr(local, "component_status", lambda: {"embedding": {"available": True}})
        monkeypatch.setattr(local, "embed_texts", lambda _texts: {"vectors": [[1.0] + [0.0] * 1023]})
    from app.data_access.research import memory_session
    from app.research import store
    from sqlalchemy import select
    with memory_session() as session:
        embedding = session.execute(select(store.ResearchEmbedding).where(store.ResearchEmbedding.chunk_id == source["id"])).scalar_one_or_none()
        if embedding is None:
            import pytest
            pytest.skip("embedding index not available")
        if not real_embedding:
            embedding.vector_json = json.dumps([1.0] + [0.0] * 1023)
    result = client.post(f"/api/research/projects/{project}/chat", headers=headers,
                         json={"message": "论文的准确率是多少？", "provider": "local", "query_confidentiality": "L3"})
    assert result.status_code == 200, result.text
    assert result.json()["data"]["sources"]
    assert "87.4%" in result.json()["data"]["answer"]
