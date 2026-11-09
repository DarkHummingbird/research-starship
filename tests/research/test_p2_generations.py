from __future__ import annotations

from pathlib import Path

import pytest

from app.research.local_components import component_status


@pytest.fixture
def study(client, auth, tmp_path, monkeypatch):
    from app.data_access import user_scope

    monkeypatch.setenv("RESEARCH_DATA_DIR", str(tmp_path / "research"))
    uid = "p2-generation-user"
    headers = auth(uid)
    with user_scope(uid):
        project = client.post("/api/research/projects", json={"name": "研究生成"}, headers=headers)
        project_id = project.json()["data"]["id"]
        baseline = client.post(
            f"/api/research/projects/{project_id}/study-notes",
            json={"title": "KeywordSearch 基线", "content": "数据集：SYNTH-10。划分：8:2。模型：KeywordSearch。指标：准确率 80.0%。", "document_type": "baseline", "confidentiality": "L3"},
            headers=headers,
        ).json()["data"]
        failure = client.post(
            f"/api/research/projects/{project_id}/study-notes",
            json={"title": "长上下文失败记录", "content": "模型：OldRAG-v1。配置：8K。失败原因：来源超出上下文后引用错位。数据集：SYNTH-10。", "document_type": "failure_experiment", "confidentiality": "L3"},
            headers=headers,
        ).json()["data"]
        for document in (baseline, failure):
            response = client.post(
                f"/api/research/documents/{document['id']}/review",
                json={"expected_version": 1, "approved": True}, headers=headers,
            )
            assert response.status_code == 200, response.text
        response = client.post(
            f"/api/research/projects/{project_id}/baseline",
            json={"document_id": baseline["id"]}, headers=headers,
        )
        assert response.status_code == 200, response.text
        yield uid, headers, project_id, baseline


def test_ablation_history_archive_and_versioned_export(client, study):
    if not component_status()["generative_llm"]["available"]:
        pytest.skip("local generative model unavailable")
    _, headers, project_id, baseline = study
    response = client.post(
        f"/api/research/projects/{project_id}/generations",
        json={"kind": "ablation", "query": "比较 KeywordSearch 与 SafeRAG，设计去除证据过滤的消融", "document_ids": [baseline["id"]]},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    generated = response.json()["data"]
    assert generated["status"] == "pending"
    assert generated["output_category"] == "agent_inference"
    assert generated["history"]["failure_experiment_count"] == 1
    assert generated["history"]["baseline_document_id"] == baseline["id"]
    assert generated["history"]["style_sample_count"] == 0
    assert generated["experiments_executed"] is False
    assert generated["sources"]

    archived = client.post(f"/api/research/generations/{generated['id']}/archive", headers=headers)
    assert archived.status_code == 201, archived.text
    document = archived.json()["data"]
    assert document["status"] == "pending"
    assert document["document_type"] == "experiment_idea"
    duplicate = client.post(f"/api/research/generations/{generated['id']}/archive", headers=headers)
    assert duplicate.status_code == 409

    first = client.post(
        f"/api/research/generations/{generated['id']}/export",
        json={"filename": "ablation.md"}, headers=headers,
    )
    assert first.status_code == 201, first.text
    second = client.post(
        f"/api/research/generations/{generated['id']}/export",
        json={"filename": "ablation.md"}, headers=headers,
    )
    assert second.status_code == 201, second.text
    assert second.json()["data"]["filename"] == "ablation-v2.md"
    blocked = client.post(
        f"/api/research/generations/{generated['id']}/export",
        json={"filename": "ablation.md", "overwrite": True}, headers=headers,
    )
    assert blocked.status_code == 409
    confirmed = client.post(
        f"/api/research/generations/{generated['id']}/export",
        json={"filename": "ablation.md", "overwrite": True, "confirm_overwrite": True}, headers=headers,
    )
    assert confirmed.status_code == 201
    assert confirmed.json()["data"]["overwritten"] is True
    assert confirmed.json()["data"]["remote_exported"] is False


def test_latex_without_style_samples_stays_source_bound(client, study):
    if not component_status()["generative_llm"]["available"]:
        pytest.skip("local generative model unavailable")
    _, headers, project_id, baseline = study
    response = client.post(
        f"/api/research/projects/{project_id}/generations",
        json={"kind": "latex", "query": "写一小段实验设置，不模仿个人风格", "document_ids": [baseline["id"]]},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    generated = response.json()["data"]
    assert generated["output_category"] == "latex_draft"
    assert generated["history"]["style_sample_count"] == 0
    assert generated["experiments_executed"] is False
    assert "80.0" in generated["content"]
    assert "请复核" in generated["content"]


def test_other_generation_types_use_explicit_categories(client, study, monkeypatch):
    import app.research.generations as generations

    monkeypatch.setattr(generations, "generate", lambda *_args, **_kwargs: {"text": "合成输出【请复核，存在幻觉风险】", "model": "test-double"})
    _, headers, project_id, baseline = study
    for kind, category in (("table", "agent_inference"), ("pseudocode", "agent_inference"), ("review", "simulated_review")):
        response = client.post(
            f"/api/research/projects/{project_id}/generations",
            json={"kind": kind, "query": "处理当前基线", "document_ids": [baseline["id"]]}, headers=headers,
        )
        assert response.status_code == 201, response.text
        assert response.json()["data"]["output_category"] == category
