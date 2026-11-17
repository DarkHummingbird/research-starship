from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import Mock

from app.gateway.config import ModelSpec
from app.research.query_planner import QUERY_PLANNER_PROMPT


SPARSE = "# Sparse Study\n\n## Method\nBM25 uses term matching and inverse document frequency.\n\n## Baseline\nDense Retrieval is the comparison baseline.\n\n## Results\nBM25 reaches 87.4% accuracy on OPEN-DEMO-2."
DENSE = "# Dense Study\n\n## Method\nDense Retrieval uses semantic vector similarity.\n\n## Baseline\nBM25 is the comparison baseline.\n\n## Results\nDense Retrieval reaches 82.1% accuracy on OPEN-DEMO-2."


def _configure_online(monkeypatch, responses):
    spec = ModelSpec(
        id="deepseek-chat", model="deepseek-chat", vendor="deepseek",
        base_url="https://api.deepseek.test/v1", api_key_env=("TEST_QUERY_PLANNER_KEY",),
    )
    monkeypatch.setenv("TEST_QUERY_PLANNER_KEY", "synthetic")
    monkeypatch.setattr("app.gateway.config.get_config", lambda: SimpleNamespace(models={spec.id: spec}))
    calls = Mock(side_effect=[SimpleNamespace(content=item) for item in responses])
    monkeypatch.setattr("app.gateway.providers.call", calls)
    return calls


def _project_with_methods(client, headers, level="L1"):
    project = client.post("/api/research/projects", json={"name": "比较查询规划"}, headers=headers).json()["data"]
    for name, content, method in (("sparse", SPARSE, "BM25"), ("dense", DENSE, "Dense Retrieval")):
        document = client.post(
            f"/api/research/projects/{project['id']}/documents",
            files={"file": (f"{name}.md", content.encode(), "text/markdown")},
            data={"confidentiality": level, "document_type": "paper"}, headers=headers,
        ).json()["data"]
        source = next(item for item in document["sources"] if item["chapter"] == "Method")
        other = "Dense Retrieval" if method == "BM25" else "BM25"
        baseline_source = next(item for item in document["sources"] if other.split()[0] in item["content"])
        metric_source = next(item for item in document["sources"] if "Results" in item["chapter"])
        payload = {
            "schema_version": "research-paper/v1", "record_type": "paper_statement",
            "method": [{
                "statement": f"{method} uses a retrieval method",
                "evidence": {"chunk_id": source["id"], "quote": source["content"]},
            }],
            "baseline": [{
                "statement": f"{other} is the comparison baseline",
                "evidence": {"chunk_id": baseline_source["id"], "quote": baseline_source["content"]},
            }], "datasets": [],
            "metrics": [{
                "statement": f"{method} reaches a synthetic accuracy", "value": "87.4" if method == "BM25" else "82.1", "unit": "%",
                "evidence": {"chunk_id": metric_source["id"], "quote": metric_source["content"]},
            }], "limitations": [], "suspicious": [],
        }
        updated = client.put(
            f"/api/research/documents/{document['id']}/analysis",
            json={"expected_version": 1, "analysis": payload}, headers=headers,
        )
        assert updated.status_code == 200, updated.text
        reviewed = client.post(
            f"/api/research/documents/{document['id']}/review",
            json={"expected_version": 1, "approved": True}, headers=headers,
        )
        assert reviewed.status_code == 200, reviewed.text
    return project


def _plan(intent="compare", entities=None):
    return json.dumps({
        "intent": intent, "entities": entities or [], "aspects": ["accuracy"],
        "needs_multiple_sources": intent == "compare", "needs_clarification": False,
    })


def test_obvious_comparison_covers_both_sides_without_planner_call(client, auth, monkeypatch):
    headers = auth("comparison-rules-user")
    project = _project_with_methods(client, headers)
    calls = _configure_online(monkeypatch, ["双方都有来源，不直接推断真实效果。"])
    response = client.post(
        f"/api/research/projects/{project['id']}/chat",
        json={"message": "比较 BM25 和 Dense Retrieval 的准确率", "provider": "external", "model_id": "deepseek-chat"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    result = response.json()["data"]
    assert calls.call_count == 1
    assert result["query_plan"]["planner"] == "rules"
    assert set(result["query_plan"]["covered"]) == {"BM25", "Dense Retrieval"}
    assert result["query_plan"]["missing"] == []
    assert {item["title"] for item in result["sources"]} == {"Sparse Study", "Dense Study"}
    assert len({item["chunk_id"] for item in result["sources"]}) == len(result["sources"])
    source_text = "\n".join(item["quote"] for item in result["sources"])
    assert "87.4%" in source_text and "82.1%" in source_text
    assert "BM25 uses term matching" in source_text
    assert "Dense Retrieval uses semantic vector similarity" in source_text, result["sources"]


def test_complex_query_uses_deepseek_planner_then_covers_sources(client, auth, monkeypatch):
    headers = auth("comparison-deepseek-planner-user")
    project = _project_with_methods(client, headers)
    calls = _configure_online(monkeypatch, [
        _plan(entities=["BM25", "Dense Retrieval"]),
        "两种方法都有来源，合成指标不能代表真实效果。",
    ])
    response = client.post(
        f"/api/research/projects/{project['id']}/chat",
        json={"message": "从效果角度评价这些方法", "provider": "external", "model_id": "deepseek-chat"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    result = response.json()["data"]
    assert calls.call_count == 2
    assert calls.call_args_list[0].kwargs["system"] == QUERY_PLANNER_PROMPT
    assert result["query_plan"]["planner"] == "deepseek-query-planner-v1"
    assert set(result["query_plan"]["covered"]) == {"BM25", "Dense Retrieval"}
    assert {item["title"] for item in result["sources"]} == {"Sparse Study", "Dense Study"}


def test_missing_comparison_entity_is_explicit(client, auth, monkeypatch):
    headers = auth("comparison-missing-user")
    project = _project_with_methods(client, headers)
    calls = _configure_online(monkeypatch, ["BM25 有来源，NotAModel 缺少资料，不能完整比较。"])
    response = client.post(
        f"/api/research/projects/{project['id']}/chat",
        json={"message": "比较 BM25 和 NotAModel 的准确率", "provider": "external", "model_id": "deepseek-chat"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    result = response.json()["data"]
    assert result["query_plan"]["covered"] == ["BM25"]
    assert result["query_plan"]["missing"] == ["NotAModel"]
    assert calls.call_count == 1
    assert all(item["title"] != "NotAModel" for item in result["sources"])


def test_l2_query_planning_and_answer_have_separate_approvals(client, auth, monkeypatch):
    headers = auth("comparison-l2-planner-user")
    project = _project_with_methods(client, headers, level="L2")
    calls = _configure_online(monkeypatch, [
        _plan(entities=["BM25", "Dense Retrieval"]),
        "双方已核对，合成结果不能外推。",
    ])
    body = {"message": "从效果角度评价这些方法", "provider": "external", "model_id": "deepseek-chat", "query_confidentiality": "L2"}
    planning = client.post(f"/api/research/projects/{project['id']}/chat", json=body, headers=headers)
    assert planning.status_code == 200, planning.text
    pending_plan = planning.json()["data"]
    assert pending_plan["approval"]["purpose"] == "科研查询规划"
    calls.assert_not_called()
    client.post(f"/api/research/outbound/{pending_plan['approval']['id']}/decision", json={"approved": True}, headers=headers)

    final_pending = client.post(
        f"/api/research/projects/{project['id']}/chat",
        json={**body, "conversation_id": pending_plan["conversation_id"], "task_id": pending_plan["task_id"], "approval_id": pending_plan["approval"]["id"]},
        headers=headers,
    )
    assert final_pending.status_code == 200, final_pending.text
    final_approval = final_pending.json()["data"]["approval"]
    assert final_approval["purpose"] == "科研课题问答"
    assert calls.call_count == 1
    client.post(f"/api/research/outbound/{final_approval['id']}/decision", json={"approved": True}, headers=headers)

    completed = client.post(
        f"/api/research/projects/{project['id']}/chat",
        json={**body, "conversation_id": pending_plan["conversation_id"], "task_id": final_approval["task_id"], "approval_id": final_approval["id"], "query_plan": final_pending.json()["data"]["query_plan"]},
        headers=headers,
    )
    assert completed.status_code == 200, completed.text
    assert completed.json()["data"]["query_plan"]["covered"]
    assert calls.call_count == 2


def test_invalid_plan_falls_back_once_and_l3_never_calls_planner(client, auth, monkeypatch):
    headers = auth("comparison-invalid-plan-user")
    project = _project_with_methods(client, headers)
    calls = _configure_online(monkeypatch, ["not-json", "普通回答"])
    response = client.post(
        f"/api/research/projects/{project['id']}/chat",
        json={"message": "评价 BM25", "provider": "external", "model_id": "deepseek-chat"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["query_plan"]["planner"] == "rules_after_invalid_plan"
    assert calls.call_count == 2

    l3_project = _project_with_methods(client, headers, level="L3")
    calls.reset_mock()
    blocked = client.post(
        f"/api/research/projects/{l3_project['id']}/chat",
        json={"message": "比较 BM25 和 Dense Retrieval", "provider": "external", "model_id": "deepseek-chat", "query_confidentiality": "L3"},
        headers=headers,
    )
    assert blocked.status_code == 403, blocked.text
    assert blocked.json()["detail"]["code"] == "l3_external_forbidden"
    calls.assert_not_called()


def test_online_rerank_preserves_comparison_representatives(client, auth, monkeypatch):
    from app.research import service

    headers = auth("comparison-rerank-user")
    project = client.post("/api/research/projects", json={"name": "重排覆盖"}, headers=headers).json()["data"]
    monkeypatch.setattr("app.research.online.rerank_spec", lambda: SimpleNamespace(
        id="rerank", vendor="deepseek", base_url="https://api.deepseek.test/v1",
        has_key=lambda: True,
    ))
    monkeypatch.setattr("app.research.online.rerank", lambda *args: [0, 1, 2])
    monkeypatch.setattr("app.research.gateway.execute_once", lambda _uid, _approval, sender: sender({}))
    matches = [{
        "document_id": f"doc-{name}", "document_version": 1, "chunk_id": f"chunk-{name}",
        "title": name, "chapter": "Method", "content": name, "confidentiality": "L1",
        "project_id": project["id"], "locator": {},
        "comparison_entities": [name],
    } for name in ("Dense Retrieval", "Dense Retrieval", "BM25")]
    matches[1]["chunk_id"] = "chunk-dense-other"
    from app.data_access import user_scope
    with user_scope("comparison-rerank-user"):
        ordered, pending = service._rerank_matches(
            "comparison-rerank-user", project["id"], "评价 BM25 和 Dense Retrieval", matches,
            "L1", "task-rerank", "", {"intent": "compare"},
        )
    assert pending is None
    assert ordered[0]["chunk_id"] == "chunk-Dense Retrieval"
    assert ordered[1]["chunk_id"] == "chunk-BM25"
    assert {item["chunk_id"] for item in ordered[:2]} == {"chunk-Dense Retrieval", "chunk-BM25"}


def test_rerank_network_failure_uses_local_order_without_retry(client, auth, monkeypatch):
    from app.gateway import providers

    headers = auth("rerank-network-fallback-user")
    project = _project_with_methods(client, headers)
    monkeypatch.setattr("app.research.online.rerank_spec", lambda: SimpleNamespace(
        id="rerank", vendor="qwen", base_url="https://api.deepseek.test/v1",
        has_key=lambda: True,
    ))
    calls = Mock(side_effect=providers.ProviderError("timeout", "synthetic slow upstream"))
    monkeypatch.setattr("app.research.online.rerank", calls)
    response = client.post(
        f"/api/research/projects/{project['id']}/chat",
        json={"message": "比较 BM25 和 Dense Retrieval 的准确率", "provider": "local"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    result = response.json()["data"]
    assert result["status"] == "answered"
    assert result["query_plan"]["rerank_status"] == "degraded_local_order"
    assert "未重试" in result["retrieval_warning"]
    assert calls.call_count == 1


def test_rerank_bad_request_does_not_fall_back(client, auth, monkeypatch):
    from app.gateway import providers

    headers = auth("rerank-bad-request-user")
    project = _project_with_methods(client, headers)
    monkeypatch.setattr("app.research.online.rerank_spec", lambda: SimpleNamespace(
        id="rerank", vendor="qwen", base_url="https://api.deepseek.test/v1",
        has_key=lambda: True,
    ))
    calls = Mock(side_effect=providers.ProviderError("bad_request", "synthetic invalid request", status=400))
    monkeypatch.setattr("app.research.online.rerank", calls)
    response = client.post(
        f"/api/research/projects/{project['id']}/chat",
        json={"message": "比较 BM25 和 Dense Retrieval 的准确率", "provider": "local"},
        headers=headers,
    )
    assert response.status_code == 502, response.text
    assert response.json()["detail"]["code"] == "external_call_failed_no_retry"
    assert calls.call_count == 1


def _add_bm25_variant(client, headers, project_id, title="Sparse Study Extended"):
    content = SPARSE.replace("Sparse Study", title, 1)
    document = client.post(
        f"/api/research/projects/{project_id}/documents",
        files={"file": ("sparse-variant.md", content.encode(), "text/markdown")},
        data={"confidentiality": "L1", "document_type": "paper"}, headers=headers,
    ).json()["data"]
    source = next(item for item in document["sources"] if item["chapter"] == "Method")
    payload = {
        "schema_version": "research-paper/v1", "record_type": "paper_statement",
        "method": [{"statement": "BM25 uses term matching", "evidence": {"chunk_id": source["id"], "quote": source["content"]}}],
        "baseline": [], "datasets": [], "metrics": [], "limitations": [], "suspicious": [],
    }
    assert client.put(
        f"/api/research/documents/{document['id']}/analysis",
        json={"expected_version": 1, "analysis": payload}, headers=headers,
    ).status_code == 200
    assert client.post(
        f"/api/research/documents/{document['id']}/review",
        json={"expected_version": 1, "approved": True}, headers=headers,
    ).status_code == 200
    return document


def test_same_method_multiple_documents_requires_user_selection(client, auth, monkeypatch):
    headers = auth("ambiguous-method-user")
    project = _project_with_methods(client, headers)
    variant = _add_bm25_variant(client, headers, project["id"])
    calls = _configure_online(monkeypatch, ["已按用户选择比较"])
    body = {"message": "比较 BM25 和 Dense Retrieval", "provider": "external", "model_id": "deepseek-chat"}
    response = client.post(f"/api/research/projects/{project['id']}/chat", json=body, headers=headers)
    assert response.status_code == 200, response.text
    result = response.json()["data"]
    assert result["status"] == "needs_clarification"
    bm25 = next(item for item in result["clarification"]["entities"] if item["name"] == "BM25")
    assert len(bm25["candidates"]) == 2
    assert variant["id"] in {item["document_id"] for item in bm25["candidates"]}
    calls.assert_not_called()

    dense = client.get(f"/api/research/projects/{project['id']}/documents", headers=headers).json()["data"]["items"]
    dense_id = next(item["id"] for item in dense if item["title"] == "Dense Study")
    selected = client.post(
        f"/api/research/projects/{project['id']}/chat",
        json={**body, "query_plan": result["query_plan"], "selected_document_ids": [variant["id"], dense_id]},
        headers=headers,
    )
    assert selected.status_code == 200, selected.text
    answered = selected.json()["data"]
    assert answered["status"] == "answered"
    assert calls.call_count == 1
    assert {item["title"] for item in answered["sources"]} <= {"Sparse Study Extended", "Dense Study"}
    assert "clarification" not in answered["query_plan"]


def test_generic_comparison_offers_candidates_and_validates_account(client, auth, monkeypatch):
    headers = auth("generic-comparison-user")
    project = _project_with_methods(client, headers)
    calls = _configure_online(monkeypatch, ["不应提前生成"])
    body = {"message": "比较这两种方法", "provider": "external", "model_id": "deepseek-chat"}
    response = client.post(f"/api/research/projects/{project['id']}/chat", json=body, headers=headers)
    result = response.json()["data"]
    assert result["status"] == "needs_clarification"
    candidates = result["clarification"]["entities"][0]["candidates"]
    assert {item["method"] for item in candidates} == {"BM25", "Dense Retrieval"}
    calls.assert_not_called()

    other_headers = auth("generic-comparison-other-user")
    forbidden = client.post(
        f"/api/research/projects/{project['id']}/chat",
        json={**body, "query_plan": result["query_plan"], "selected_document_ids": [item["document_id"] for item in candidates]},
        headers=other_headers,
    )
    assert forbidden.status_code == 404
    calls.assert_not_called()
