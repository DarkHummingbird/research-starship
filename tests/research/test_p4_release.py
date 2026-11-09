from __future__ import annotations

import hashlib
from pathlib import Path

from app.research import gateway, service


P0_ROOT = Path(__file__).resolve().parents[3]
TUTORIAL = (P0_ROOT / "测试样本" / "虚构Agent使用教程.md").read_bytes()


def _project(client, headers, name):
    response = client.post("/api/research/projects", json={"name": name}, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["data"]["id"]


def _tutorial(client, headers, project_id, level="L2"):
    response = client.post(
        f"/api/research/projects/{project_id}/documents",
        files={"file": ("p4-tutorial.md", TUTORIAL, "text/markdown")},
        data={"confidentiality": level, "document_type": "tutorial"}, headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


def _fragment(document, index=0):
    entry = document["parsed"]["entries"][index]
    content = entry["statement"]
    return {
        "document_id": document["id"], "version": document["version"],
        "chunk_id": entry["evidence"]["chunk_id"], "chapter": entry["evidence"]["chapter"],
        "snippet": content, "classification": document["confidentiality"],
        "sha256": hashlib.sha256(content.encode()).hexdigest(),
    }


def test_ac10_new_material_in_same_l2_task_requires_new_approval(client, auth, tmp_path, monkeypatch):
    monkeypatch.setenv("RESEARCH_DATA_DIR", str(tmp_path / "research"))
    uid = "p4-added-material-user"
    headers = auth(uid)
    project_id = _project(client, headers, "AC10 新增材料")
    first = _tutorial(client, headers, project_id)
    second = _tutorial(client, headers, project_id)
    from app.data_access import user_scope

    with user_scope(uid):
        initial = gateway.prepare(
            uid, project_id, task_id="same-l2-task", provider="deepseek", model_id="test",
            destination="https://api.deepseek.test/v1", purpose="科研问答", operation="qa",
            fragments=[_fragment(first)],
        )
        gateway.decide(uid, initial["id"], True)
        expanded = gateway.prepare(
            uid, project_id, task_id="same-l2-task", provider="deepseek", model_id="test",
            destination="https://api.deepseek.test/v1", purpose="科研问答", operation="qa",
            fragments=[_fragment(first), _fragment(second)],
        )
        assert expanded["id"] != initial["id"]
        assert expanded["status"] == "pending"
        assert gateway.get(uid, initial["id"])["status"] == "invalidated"
        calls = []
        try:
            gateway.execute_once(uid, initial["id"], lambda approval: calls.append(approval) or "must not send")
        except gateway.PolicyError as exc:
            assert exc.code == "outbound_not_approved"
        else:
            raise AssertionError("invalidated approval was executable")
        assert calls == []


def test_ac12_document_parse_failure_stops_without_retry(client, auth, tmp_path, monkeypatch):
    monkeypatch.setenv("RESEARCH_DATA_DIR", str(tmp_path / "research"))
    headers = auth("p4-parse-failure-user")
    project_id = _project(client, headers, "AC12 解析失败")
    calls = []

    def fail_once(*_args, **_kwargs):
        calls.append(1)
        raise ValueError("synthetic malformed document")

    monkeypatch.setattr("app.research.ingest.parse_input", fail_once)
    response = client.post(
        f"/api/research/projects/{project_id}/documents",
        files={"file": ("broken.pdf", b"not-a-pdf", "application/pdf")},
        data={"confidentiality": "L3", "document_type": "paper"}, headers=headers,
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "document_parse_failed"
    assert len(calls) == 1
    assert client.get(f"/api/research/projects/{project_id}/documents", headers=headers).json()["data"]["items"] == []


def test_ac14_research_external_call_has_no_amount_cap_or_reminder(client, auth, tmp_path, monkeypatch):
    monkeypatch.setenv("RESEARCH_DATA_DIR", str(tmp_path / "research"))
    monkeypatch.setenv("AGENT_MAX_COST", "0")
    monkeypatch.setenv("AGENT_COST_SOFT_LIMIT", "0")
    monkeypatch.setenv("AGENT_COST_CAP_PAID_MONTHLY", "0.01")
    headers = auth("p4-no-cost-cap-user")
    project_id = _project(client, headers, "AC14 无金额门槛")
    document = _tutorial(client, headers, project_id, "L1")
    reviewed = client.post(
        f"/api/research/documents/{document['id']}/review",
        json={"expected_version": 1, "approved": True}, headers=headers,
    )
    assert reviewed.status_code == 200, reviewed.text
    monkeypatch.setattr(service, "_model_destination", lambda _model_id: ("deepseek", "https://api.deepseek.test/v1"))
    calls = []
    monkeypatch.setattr(gateway, "configured_model_sender", lambda approval: calls.append(approval) or "公开资料模拟回答")
    response = client.post(
        f"/api/research/projects/{project_id}/chat",
        json={"message": "默认搜索哪个课题？", "provider": "external", "model_id": "public-test"}, headers=headers,
    )
    assert response.status_code == 200, response.text
    result = response.json()["data"]
    assert result["status"] == "answered"
    assert len(calls) == 1
    assert "cost_cap" not in result["answer"]
    assert "费用" not in result["answer"] and "金额" not in result["answer"]


def test_ac12_public_retries_twice_but_approved_l2_failure_does_not_retry(client, auth, tmp_path, monkeypatch):
    import httpx
    import pytest
    from app.data_access import user_scope
    from app.research import external_sources

    monkeypatch.setenv("RESEARCH_DATA_DIR", str(tmp_path / "research"))
    uid = "p4-source-retry-user"
    headers = auth(uid)
    project_id = _project(client, headers, "AC12 来源重试")
    calls = []

    def fail(*_args, **_kwargs):
        calls.append(1)
        raise httpx.ConnectError("synthetic source outage")

    monkeypatch.setattr(external_sources.httpx, "get", fail)
    sleeps = []
    monkeypatch.setattr(external_sources.time, "sleep", sleeps.append)
    with user_scope(uid):
        with pytest.raises(gateway.PolicyError) as public_failed:
            external_sources.search_arxiv(uid, project_id, query="transformer", level="L1")
        assert public_failed.value.code == "external_call_failed_no_retry"
        assert public_failed.value.data["error_type"] == "ConnectError"
        assert len(calls) == 3
        assert sleeps == [2.0, 5.0]

        pending = external_sources.search_arxiv(uid, project_id, query="transformer", level="L2")
        assert pending["status"] == "awaiting_outbound_approval"
        assert len(calls) == 3
        approval = pending["approval"]
        gateway.decide(uid, approval["id"], True)
        with pytest.raises(gateway.PolicyError) as failed:
            external_sources.search_arxiv(
                uid, project_id, query="transformer", level="L2",
                task_id=approval["task_id"], approval_id=approval["id"],
            )
        assert failed.value.code == "external_call_failed_no_retry"
        assert len(calls) == 4
        assert sleeps == [2.0, 5.0]


def test_ac17_cross_project_derivative_inherits_highest_source_level(client, auth, tmp_path, monkeypatch):
    monkeypatch.setenv("RESEARCH_DATA_DIR", str(tmp_path / "research"))
    headers = auth("p4-derived-level-user")
    source_project = _project(client, headers, "AC17 来源课题")
    target_project = _project(client, headers, "AC17 当前课题")
    document = _tutorial(client, headers, source_project, "L3")
    reviewed = client.post(
        f"/api/research/documents/{document['id']}/review",
        json={"expected_version": 1, "approved": True}, headers=headers,
    )
    assert reviewed.status_code == 200, reviewed.text
    isolated = client.post(
        f"/api/research/projects/{target_project}/chat",
        json={"message": "默认搜索哪个课题？", "provider": "local", "query_confidentiality": "L1"}, headers=headers,
    )
    assert isolated.status_code == 200
    assert isolated.json()["data"]["sources"] == []
    response = client.post(
        f"/api/research/projects/{target_project}/chat",
        json={"message": "默认搜索哪个课题？", "provider": "local", "query_confidentiality": "L1", "cross_project_ids": [source_project]}, headers=headers,
    )
    assert response.status_code == 200, response.text
    result = response.json()["data"]
    assert result["confidentiality"] == "L3"
    assert result["sources"][0]["confidentiality"] == "L3"
    assert result["sources"][0]["locator"]["chapter"] == "3. 搜索与提问"
    assert result["scope_project_ids"] == [target_project, source_project]
