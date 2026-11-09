from __future__ import annotations

import json

import pytest

from app.research.local_components import component_status


@pytest.fixture
def paper(client, auth, tmp_path, monkeypatch):
    from app.data_access import user_scope

    monkeypatch.setenv("RESEARCH_DATA_DIR", str(tmp_path / "research"))
    uid = "p2-analysis-user"
    headers = auth(uid)
    with user_scope(uid):
        fitz = pytest.importorskip("fitz")
        pdf = fitz.open()
        page = pdf.new_page(width=900, height=900)
        text = (
            "Synthetic Retrieval Study\n\n"
            "1. Method\nWe use a synthetic retrieval method named SafeRAG.\n\n"
            "2. Baseline\nBaseline: KeywordSearch.\n\n"
            "3. Dataset\nDataset: SYNTH-10 containing 10 fictional documents.\n\n"
            "4. Results\nSafeRAG reaches 87.4% accuracy on SYNTH-10.\n\n"
            "5. Limitations\nThe evaluation is limited to fictional examples."
        )
        page.insert_text((50, 50), text, fontsize=11)
        content = pdf.tobytes()
        pdf.close()
        project = client.post("/api/research/projects", json={"name": "论文分析"}, headers=headers)
        project_id = project.json()["data"]["id"]
        response = client.post(
            f"/api/research/projects/{project_id}/documents",
            files={"file": ("synthetic-paper.pdf", content, "application/pdf")},
            data={"confidentiality": "L3", "document_type": "paper"}, headers=headers,
        )
        assert response.status_code == 201, response.text
        yield uid, headers, project_id, response.json()["data"]


def test_l3_paper_analysis_review_and_memory(client, paper):
    if not component_status()["generative_llm"]["available"]:
        pytest.skip("local generative model unavailable")
    _, headers, project_id, document = paper
    response = client.post(f"/api/research/documents/{document['id']}/analysis", headers=headers)
    assert response.status_code == 200, response.text
    result = response.json()["data"]
    assert result["analysis_status"] == "ready"
    analysis = result["analysis"]
    assert analysis["schema_version"] == "research-paper/v1"
    for field in ("method", "baseline", "datasets", "metrics", "limitations"):
        assert analysis[field], field
        for fact in analysis[field]:
            assert isinstance(fact["statement"], str)
            assert isinstance(fact["evidence"]["quote"], str)
            assert not isinstance(fact.get("value"), (dict, list))
    approved = client.post(
        f"/api/research/documents/{document['id']}/review",
        json={"expected_version": 1, "approved": True}, headers=headers,
    )
    assert approved.status_code == 200, approved.text
    memories = client.get(f"/api/research/projects/{project_id}/memories", headers=headers).json()["data"]["items"]
    entry_ids = {item["entry_id"] for item in memories}
    assert {"method-1", "baseline-1", "datasets-1", "metrics-1", "limitations-1"} <= entry_ids


def test_analysis_rejects_object_metric_and_fabricated_source(client, paper):
    _, headers, _, document = paper
    source = document["sources"][0]
    payload = {
        "schema_version": "research-paper/v1", "record_type": "paper_statement",
        "method": [], "baseline": [], "datasets": [],
        "metrics": [{
            "statement": "虚构指标", "value": {"accuracy": 99.9}, "unit": None,
            "evidence": {"chunk_id": source["id"], "quote": "not in source"},
        }],
        "limitations": [], "suspicious": [],
    }
    response = client.put(
        f"/api/research/documents/{document['id']}/analysis",
        json={"expected_version": 1, "analysis": payload}, headers=headers,
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "paper_analysis_invalid"
    payload["metrics"][0]["value"] = 99.9
    response = client.put(
        f"/api/research/documents/{document['id']}/analysis",
        json={"expected_version": 1, "analysis": payload}, headers=headers,
    )
    assert response.status_code == 422
