from __future__ import annotations

import json
import time
from types import SimpleNamespace
from unittest.mock import Mock

from app.data_access import user_scope
from app.gateway.config import ModelSpec
from app.research import local_mode


PAPER = """# Synthetic Retrieval Study

## Method
SafeRAG uses source-checked retrieval.

## Results
SafeRAG reaches 87.4% accuracy on SYNTH-10.

## Limitations
The evaluation is limited to fictional examples.
"""


def _set_local_mode(user_id: str, enabled: bool) -> None:
    with user_scope(user_id):
        local_mode.set_enabled(user_id, enabled)


def _online(monkeypatch, response=None):
    spec = ModelSpec(
        id="deepseek-chat", model="deepseek-chat", vendor="deepseek",
        base_url="https://api.deepseek.test/v1", api_key_env=("TEST_ONLINE_GENERATION_KEY",),
    )
    monkeypatch.setenv("TEST_ONLINE_GENERATION_KEY", "synthetic")
    monkeypatch.setattr("app.gateway.config.get_config", lambda: SimpleNamespace(models={spec.id: spec}))
    calls = Mock(return_value=SimpleNamespace(content=response or "在线研究辅助设想【请复核，存在幻觉风险】"))
    monkeypatch.setattr("app.gateway.providers.call", calls)
    return calls


def _project(client, headers, name="在线科研功能"):
    return client.post("/api/research/projects", json={"name": name}, headers=headers).json()["data"]


def _upload(client, headers, project_id, *, level="L1", paper=True):
    response = client.post(
        f"/api/research/projects/{project_id}/documents",
        files={"file": ("synthetic.md", PAPER.encode(), "text/markdown")},
        data={"confidentiality": level, "document_type": "paper" if paper else "tutorial"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


def _review(client, headers, document):
    response = client.post(
        f"/api/research/documents/{document['id']}/review",
        json={"expected_version": document["version"], "approved": True}, headers=headers,
    )
    assert response.status_code == 200, response.text


def _analysis_payload(document):
    source = document["sources"][0]
    fact = {"statement": "SafeRAG 使用来源校验检索", "evidence": {"chunk_id": source["id"], "quote": "SafeRAG uses source-checked retrieval."}}
    return json.dumps({
        "schema_version": "research-paper/v1", "record_type": "paper_statement",
        "method": [fact], "baseline": [], "datasets": [], "metrics": [], "limitations": [], "suspicious": [],
    })


def _wait_batch(client, headers, batch_id, status):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = client.get(f"/api/research/batches/{batch_id}", headers=headers).json()["data"]
        if result["items"][0]["status"] == status:
            return result
        if result["items"][0]["status"] == "failed":
            raise AssertionError(result)
        time.sleep(0.02)
    raise AssertionError(f"batch did not reach {status}")


def _wait_async(client, headers, task_id, status):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        tasks = client.get("/api/research/tasks", headers=headers).json()["data"]["async_items"]
        result = next(item for item in tasks if item["id"] == task_id)
        if result["status"] == status:
            return result
        if result["status"] in {"failed", "unknown"}:
            raise AssertionError(result)
        time.sleep(0.02)
    raise AssertionError(f"task did not reach {status}")


def test_l1_paper_analysis_defaults_to_deepseek_with_local_mode_off(client, auth, monkeypatch):
    uid = "online-paper-l1"
    headers = auth(uid)
    _set_local_mode(uid, False)
    project = _project(client, headers)
    document = _upload(client, headers, project["id"])
    calls = _online(monkeypatch, _analysis_payload(document))
    local = Mock(side_effect=AssertionError("不得调用本地模型"))
    monkeypatch.setattr("app.research.paper_analysis.generate", local)

    response = client.post(f"/api/research/documents/{document['id']}/analysis", json={}, headers=headers)
    assert response.status_code == 200, response.text
    result = response.json()["data"]
    assert result["analysis_status"] == "ready"
    assert result["extraction_model"] == "external:deepseek-chat"
    assert calls.call_count == 1
    assert calls.call_args.args[0].vendor == "deepseek"
    local.assert_not_called()


def test_l2_paper_batch_waits_for_approval_then_continues(client, auth, monkeypatch):
    uid = "online-paper-l2"
    headers = auth(uid)
    _set_local_mode(uid, False)
    project = _project(client, headers)
    document = _upload(client, headers, project["id"], level="L2")
    calls = _online(monkeypatch, _analysis_payload(document))
    batch = client.post(
        f"/api/research/projects/{project['id']}/batches",
        json={"document_ids": [document["id"]]}, headers=headers,
    ).json()["data"]
    pending = _wait_batch(client, headers, batch["id"], "awaiting_approval")
    assert pending["summary"]["awaiting_approval"] == 1
    assert pending["items"][0]["status"] == "awaiting_approval"
    approval_id = pending["items"][0]["approval_id"]
    approval = client.get(f"/api/research/outbound/{approval_id}", headers=headers).json()["data"]
    assert approval["confidentiality"] == "L2"
    assert approval["purpose"] == "论文结构化分析"
    calls.assert_not_called()

    decided = client.post(f"/api/research/outbound/{approval_id}/decision", json={"approved": True}, headers=headers)
    assert decided.status_code == 200
    resumed = client.post(
        f"/api/research/batch-items/{pending['items'][0]['id']}/retry",
        json={"approval_id": approval_id}, headers=headers,
    )
    assert resumed.status_code == 202, resumed.text
    completed = _wait_batch(client, headers, batch["id"], "pending_review")
    assert completed["summary"]["pending_review"] == 1
    assert calls.call_count == 1
    detail = client.get(f"/api/research/documents/{document['id']}", headers=headers).json()["data"]
    assert detail["analysis_status"] == "ready"
    assert detail["extraction_model"] == "external:deepseek-chat"


def test_l2_research_assistance_async_approval_and_online_failure(client, auth, monkeypatch):
    uid = "online-research-assistance-l2"
    headers = auth(uid)
    _set_local_mode(uid, False)
    project = _project(client, headers)
    document = _upload(client, headers, project["id"], level="L2", paper=False)
    _review(client, headers, document)
    calls = _online(monkeypatch)
    local = Mock(side_effect=AssertionError("不得切换本地模型"))
    monkeypatch.setattr("app.research.generations.generate", local)

    created = client.post(
        f"/api/research/projects/{project['id']}/generation-tasks",
        json={"kind": "ablation", "query": "SafeRAG 消融", "provider": "external", "model_id": "deepseek-chat"},
        headers=headers,
    )
    assert created.status_code == 202, created.text
    task_id = created.json()["data"]["id"]
    pending = _wait_async(client, headers, task_id, "awaiting_approval")
    approval = pending["result"]["approval"]
    assert approval["purpose"] == "研究辅助：ablation"
    calls.assert_not_called()
    client.post(f"/api/research/outbound/{approval['id']}/decision", json={"approved": True}, headers=headers)
    resumed = client.post(f"/api/research/async-tasks/{task_id}/resume", json={}, headers=headers)
    assert resumed.status_code == 202, resumed.text
    completed = _wait_async(client, headers, task_id, "completed")
    assert completed["result"]["model"] == "external:deepseek-chat"
    assert calls.call_count == 1
    local.assert_not_called()

    failed_calls = _online(monkeypatch)
    failed_calls.side_effect = RuntimeError("synthetic DeepSeek outage")
    failed = client.post(
        f"/api/research/projects/{project['id']}/generations",
        json={"kind": "review", "query": "检查证据缺口", "provider": "external", "model_id": "deepseek-chat"},
        headers=headers,
    )
    assert failed.status_code == 502, failed.text
    assert failed.json()["detail"]["code"] == "external_call_failed_no_retry"
    assert failed_calls.call_count == 1
    local.assert_not_called()


def test_l3_paper_and_research_assistance_reject_online_model(client, auth, monkeypatch):
    uid = "online-l3-rejected"
    headers = auth(uid)
    project = _project(client, headers, "L3 在线拒绝")
    paper = _upload(client, headers, project["id"], level="L3")
    tutorial = _upload(client, headers, project["id"], level="L3", paper=False)
    _review(client, headers, tutorial)
    calls = _online(monkeypatch)

    analysis = client.post(f"/api/research/documents/{paper['id']}/analysis", json={}, headers=headers)
    generated = client.post(
        f"/api/research/projects/{project['id']}/generations",
        json={"kind": "ablation", "query": "SafeRAG", "provider": "external", "model_id": "deepseek-chat"},
        headers=headers,
    )
    for response in (analysis, generated):
        assert response.status_code == 403, response.text
        assert response.json()["detail"]["code"] == "l3_external_forbidden"
        assert "本地" in response.json()["detail"]["message"]
    calls.assert_not_called()
