from __future__ import annotations

import pytest

from app.research.service import _degraded_without_overlap, _terms


def test_symbol_tokens_share_one_form():
    assert "dmodel" in _terms("d_model")
    assert "dmodel" in _terms("dmodel")
    assert "dmodel" in _terms("D_Model")
    assert "beta" in _terms("β")
    assert "open-demo-2" in _terms("OPEN-DEMO-2")
    assert "opendemo2" in _terms("OPEN-DEMO-2")


def test_degraded_without_overlap_rule():
    matches = [{"matched_terms": []}]
    plan = {"embedding_status": "degraded_local_lexical"}
    assert _degraded_without_overlap(plan, matches, "quantum decoherence") is True
    assert _degraded_without_overlap({}, matches, "quantum decoherence") is False
    assert _degraded_without_overlap(plan, [], "quantum decoherence") is False
    assert _degraded_without_overlap(plan, [{"matched_terms": ["quantum"]}], "quantum decoherence") is False
    assert _degraded_without_overlap({"rerank_status": "degraded_local_order"}, matches, "quantum decoherence") is True


def test_degraded_retrieval_does_not_let_model_invent_absence(client, auth, monkeypatch):
    from app.research.local_components import component_status

    if not component_status()["embedding"]["available"]:
        pytest.skip("local embedding assets unavailable")
    headers = auth("degraded-answer-user")
    project = client.post("/api/research/projects", json={"name": "降级不硬答"}, headers=headers).json()["data"]
    content = "# 检索说明\n\n## 规则\n只有已确认的资料才能进入正式检索。\n\n## 流程\n上传后先核对原文，再决定是否保存。"
    document = client.post(
        f"/api/research/projects/{project['id']}/documents",
        files={"file": ("rules.md", content.encode(), "text/markdown")},
        data={"confidentiality": "L1", "document_type": "tutorial"}, headers=headers,
    ).json()["data"]
    reviewed = client.post(
        f"/api/research/documents/{document['id']}/review",
        json={"expected_version": 1, "approved": True}, headers=headers,
    )
    assert reviewed.status_code == 200, reviewed.text

    def degraded(user_id, project_id, message, matches, confidentiality, task_id, approval_id, query_plan=None):
        query_plan["rerank_status"] = "degraded_local_order"
        query_plan["degradation_reasons"] = [{"stage": "rerank", "reason": "timeout"}]
        return matches, None

    monkeypatch.setattr("app.research.service._rerank_matches", degraded)
    response = client.post(
        f"/api/research/projects/{project['id']}/chat",
        json={"message": "quantum decoherence in superconducting qubits", "provider": "local", "query_confidentiality": "L1"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    result = response.json()["data"]
    assert result["model"] == "degraded-no-answer-v1"
    assert "无法可靠回答" in result["answer"]
    assert "不会据此断言资料中没有" in result["answer"]