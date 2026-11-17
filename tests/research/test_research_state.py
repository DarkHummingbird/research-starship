from __future__ import annotations

import pytest


def project(client, headers, name="研究状态课题"):
    response = client.post("/api/research/projects", json={"name": name, "goal": "验证课题知识复用"}, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["data"]


def approved_document(client, headers, project_id, monkeypatch, title="BM25 retrieval"):
    from app.research import ingest
    monkeypatch.setattr(ingest, "embed_texts", lambda texts: {
        "vectors": [[1.0] + [0.0] * 1023 for _ in texts], "model": "synthetic-online-embedding", "dimensions": 1024,
    })
    content = f"# {title}\n\nBM25 retrieval uses sparse evidence and document length normalization.".encode()
    imported = client.post(
        f"/api/research/projects/{project_id}/documents",
        files={"file": ("paper.md", content, "text/markdown")},
        data={"document_type": "tutorial", "confidentiality": "L2"}, headers=headers,
    )
    assert imported.status_code == 201, imported.text
    document = imported.json()["data"]
    reviewed = client.post(
        f"/api/research/documents/{document['id']}/review",
        json={"expected_version": document["version"], "approved": True}, headers=headers,
    )
    assert reviewed.status_code == 200, reviewed.text
    detail = client.get(f"/api/research/documents/{document['id']}", headers=headers)
    assert detail.status_code == 200, detail.text
    return detail.json()["data"]


def test_state_entries_terms_and_experiment_resume(client, auth):
    headers = auth("research-state-user")
    item = project(client, headers)
    project_id = item["id"]
    state = client.patch(f"/api/research/projects/{project_id}/state", json={
        "research_question": "混合检索是否改善论文问答？",
        "next_action": "核对固定评测集", "expected_updated_at": item["updated_at"],
    }, headers=headers)
    assert state.status_code == 200, state.text
    term = client.post(f"/api/research/projects/{project_id}/state/entries", json={
        "kind": "term", "content": "Best Matching 25", "abbreviation": "BM25",
        "definition": "基于词频、逆文档频率和文档长度的排序方法",
    }, headers=headers)
    assert term.status_code == 201, term.text
    todo = client.post(f"/api/research/projects/{project_id}/state/entries", json={
        "kind": "todo", "content": "复核固定集中的三处引用",
    }, headers=headers)
    assert todo.status_code == 201, todo.text
    question = client.post(f"/api/research/projects/{project_id}/state/entries", json={
        "kind": "open_question", "content": "生成式回答的弱项来自检索还是生成？",
    }, headers=headers).json()["data"]
    stale = client.patch(f"/api/research/state-entries/{question['id']}", json={
        "expected_version": 1, "status": "resolved",
    }, headers=headers)
    assert stale.status_code == 200, stale.text
    assert stale.json()["data"]["status"] == "resolved"
    assert stale.json()["data"]["version"] == 2
    stale = client.patch(f"/api/research/state-entries/{question['id']}", json={
        "expected_version": 1, "status": "active",
    }, headers=headers)
    assert stale.status_code == 409

    experiment = client.post(f"/api/research/projects/{project_id}/experiments", json={
        "title": "有无 BM25 对照", "hypothesis": "加入 BM25 能改善检索",
        "variables": "BM25 权重", "controls": "固定资料、查询和在线生成模型",
    }, headers=headers)
    assert experiment.status_code == 201, experiment.text
    experiment_id = experiment.json()["data"]["id"]
    assert experiment.json()["data"]["experiments_executed"] is False
    updated = client.patch(f"/api/research/experiments/{experiment_id}", json={
        "expected_version": 1, "status": "succeeded",
        "fields": {"result": "固定集上混合检索优于纯向量", "next_step": "复核生成式弱项"},
    }, headers=headers)
    assert updated.status_code == 200, updated.text
    assert updated.json()["data"]["status"] == "succeeded"
    assert updated.json()["data"]["experiments_executed"] is True

    resumed = client.get(f"/api/research/projects/{project_id}/state", headers=headers)
    assert resumed.status_code == 200, resumed.text
    data = resumed.json()["data"]
    assert data["project"]["research_question"] == "混合检索是否改善论文问答？"
    assert data["project"]["next_action"] == "核对固定评测集"
    assert data["term"][0]["content"] == "Best Matching 25"
    assert data["term"][0]["abbreviation"] == "BM25"
    assert data["todo"][0]["content"] == "复核固定集中的三处引用"
    assert data["counts"]["experiments"] == 1
    assert data["experiment_summary"]["succeeded"] == 1


def test_state_and_experiment_are_private(client, auth):
    headers = auth("state-owner")
    item = project(client, headers)
    other = auth("state-other-user")
    assert other and client.get(f"/api/research/projects/{item['id']}/state", headers=other).status_code == 404
    experiment = client.post(f"/api/research/projects/{item['id']}/experiments", json={"title": "私有实验"}, headers=headers).json()["data"]
    assert client.patch(f"/api/research/experiments/{experiment['id']}", json={
        "expected_version": 1, "status": "succeeded",
    }, headers=other).status_code == 404


def test_source_change_marks_state_entry_for_review(client, auth, monkeypatch):
    headers = auth("state-source-user")
    item = project(client, headers)
    source = approved_document(client, headers, item["id"], monkeypatch)
    chunk = source["sources"][0]
    entry = client.post(f"/api/research/projects/{item['id']}/state/entries", json={
        "kind": "evidence_gap", "content": "需要核对文档长度归一化的影响",
        "source_refs": [{"document_id": source["id"], "version": source["version"],
                         "chunk_id": chunk["id"], "quote": chunk["content"][:300], "locator": chunk["locator"]}],
    }, headers=headers)
    assert entry.status_code == 201, entry.text
    changed = client.patch(f"/api/research/documents/{source['id']}", json={
        "expected_version": source["version"], "title": "BM25 retrieval updated",
    }, headers=headers)
    assert changed.status_code == 200, changed.text
    state = client.get(f"/api/research/projects/{item['id']}/state", headers=headers).json()["data"]
    assert state["evidence_gap"][0]["status"] == "pending_review"
    assert state["counts"]["pending_review"] == 1


def test_new_related_document_creates_notice_without_invalidating_note(client, auth, monkeypatch):
    headers = auth("related-review-user")
    item = project(client, headers)
    source = approved_document(client, headers, item["id"], monkeypatch, title="BM25 retrieval baseline")
    chunk = source["sources"][0]
    note = client.post(f"/api/research/projects/{item['id']}/study-notes", json={
        "title": "BM25 retrieval baseline", "content": "BM25 retrieval uses sparse evidence.",
        "document_type": "research_note", "confidentiality": "L2", "note_type": "source_fact",
        "source_refs": [{"document_id": source["id"], "version": source["version"],
                         "chunk_id": chunk["id"], "quote": chunk["content"][:300], "locator": chunk["locator"]}],
    }, headers=headers)
    assert note.status_code == 201, note.text
    note_data = note.json()["data"]
    reviewed = client.post(f"/api/research/documents/{note_data['id']}/review", json={
        "expected_version": note_data["version"], "approved": True,
    }, headers=headers)
    assert reviewed.status_code == 200, reviewed.text
    new_document = approved_document(client, headers, item["id"], monkeypatch, title="BM25 retrieval new evidence")
    assert new_document["status"] == "approved"
    state = client.get(f"/api/research/projects/{item['id']}/state", headers=headers).json()["data"]
    assert len(state["review_notice"]) == 1
    assert state["review_notice"][0]["related"]["note_id"] == note_data["id"]
    current_note = client.get(f"/api/research/documents/{note_data['id']}", headers=headers).json()["data"]
    assert current_note["status"] == "approved"


def _source_ref(source):
    chunk = source["sources"][0]
    return {
        "document_id": source["id"], "version": source["version"], "chunk_id": chunk["id"],
        "quote": chunk["content"][:300], "locator": chunk["locator"],
    }


def test_archiving_entry_keeps_snapshot_after_source_deletion(client, auth, monkeypatch):
    headers = auth("archive-deleted-source-user")
    item = project(client, headers)
    other = auth("archive-deleted-source-other")
    source = approved_document(client, headers, item["id"], monkeypatch)
    entry = client.post(f"/api/research/projects/{item['id']}/state/entries", json={
        "kind": "evidence_gap", "content": "废弃实验使用的历史证据", "source_refs": [_source_ref(source)],
    }, headers=headers).json()["data"]
    deleted = client.post(f"/api/research/documents/{source['id']}/delete", json={
        "expected_version": source["version"], "confirmed": True,
    }, headers=headers)
    assert deleted.status_code == 200, deleted.text
    denied = client.patch(f"/api/research/state-entries/{entry['id']}", json={
        "expected_version": entry["version"] + 1, "status": "archived",
    }, headers=other)
    assert denied.status_code == 404
    archived = client.patch(f"/api/research/state-entries/{entry['id']}", json={
        "expected_version": entry["version"] + 1, "status": "archived",
    }, headers=headers)
    assert archived.status_code == 200, archived.text
    assert archived.json()["data"]["status"] == "archived"
    assert archived.json()["data"]["source_refs"][0]["document_id"] == source["id"]


def _experiment_and_baseline(client, headers, item, monkeypatch):
    source = approved_document(client, headers, item["id"], monkeypatch, title="Experiment result source")
    experiment = client.post(f"/api/research/projects/{item['id']}/experiments", json={
        "title": "来源复核实验", "hypothesis": "保留原结果但标记复核",
        "variables": "检索方式", "controls": "固定数据", "data_version": "data-v1", "code_version": "code-v1",
    }, headers=headers).json()["data"]
    updated = client.patch(f"/api/research/experiments/{experiment['id']}", json={
        "expected_version": experiment["version"], "status": "succeeded",
        "fields": {"result": "用户回填的实验结果"}, "result_source_refs": [_source_ref(source)],
    }, headers=headers)
    assert updated.status_code == 200, updated.text
    experiment = updated.json()["data"]
    baseline = client.post(f"/api/research/projects/{item['id']}/study-notes", json={
        "title": "来源关联基线", "content": "用户确认的基线，不因来源变化自动替换",
        "document_type": "baseline", "confidentiality": "L2", "note_type": "source_fact",
        "source_refs": [_source_ref(source)],
    }, headers=headers)
    assert baseline.status_code == 201, baseline.text
    baseline = baseline.json()["data"]
    reviewed = client.post(f"/api/research/documents/{baseline['id']}/review", json={
        "expected_version": baseline["version"], "approved": True,
    }, headers=headers)
    assert reviewed.status_code == 200, reviewed.text
    selected = client.post(f"/api/research/projects/{item['id']}/baseline", json={
        "document_id": baseline["id"],
    }, headers=headers)
    assert selected.status_code == 200, selected.text
    return source, experiment, baseline


@pytest.mark.parametrize("change", ["update", "withdraw", "restore", "delete"])
def test_source_changes_mark_experiment_and_baseline_for_review(client, auth, monkeypatch, change):
    headers = auth(f"experiment-source-{change}-user")
    item = project(client, headers)
    source, experiment, baseline = _experiment_and_baseline(client, headers, item, monkeypatch)
    if change == "restore":
        updated = client.patch(f"/api/research/documents/{source['id']}", json={
            "expected_version": 1, "title": "Second approved source version",
        }, headers=headers)
        assert updated.status_code == 200, updated.text
        approved = client.post(f"/api/research/documents/{source['id']}/review", json={
            "expected_version": 2, "approved": True,
        }, headers=headers)
        assert approved.status_code == 200, approved.text
        source = client.get(f"/api/research/documents/{source['id']}", headers=headers).json()["data"]
        refreshed_experiment = client.patch(f"/api/research/experiments/{experiment['id']}", json={
            "expected_version": experiment["version"] + 1, "status": "succeeded",
            "result_source_refs": [_source_ref(source)],
        }, headers=headers)
        assert refreshed_experiment.status_code == 200, refreshed_experiment.text
        experiment = refreshed_experiment.json()["data"]
        refreshed_baseline = client.post(f"/api/research/documents/{baseline['id']}/review", json={
            "expected_version": 1, "approved": True,
        }, headers=headers)
        assert refreshed_baseline.status_code == 200, refreshed_baseline.text
        changed = client.post(f"/api/research/documents/{source['id']}/restore-version", json={
            "target_version": 1, "expected_version": 2, "confirmed": True,
        }, headers=headers)
    elif change == "update":
        changed = client.patch(f"/api/research/documents/{source['id']}", json={
            "expected_version": 1, "title": "Changed experiment evidence",
        }, headers=headers)
    elif change == "withdraw":
        changed = client.post(f"/api/research/documents/{source['id']}/review", json={
            "expected_version": 1, "approved": False,
        }, headers=headers)
    else:
        changed = client.post(f"/api/research/documents/{source['id']}/delete", json={
            "expected_version": 1, "confirmed": True,
        }, headers=headers)
    assert changed.status_code == 200, changed.text
    state = client.get(f"/api/research/projects/{item['id']}/state", headers=headers).json()["data"]
    current = next(row for row in state["experiments"] if row["id"] == experiment["id"])
    assert current["status"] == "pending_review"
    assert current["status_before_review"] == "succeeded"
    assert current["result"] == "用户回填的实验结果"
    assert current["review_reason"]
    assert state["pending_experiments"][0]["id"] == experiment["id"]
    assert state["baseline"]["id"] == baseline["id"]
    assert state["baseline"]["review_required"] is True
    assert state["baseline"]["validity"] in {"source_changed", "source_withdrawn"}
    assert state["baseline"]["review_reason"]
    if change == "update":
        reapproved = client.post(f"/api/research/documents/{source['id']}/review", json={
            "expected_version": 2, "approved": True,
        }, headers=headers)
        assert reapproved.status_code == 200, reapproved.text
        reviewed = client.patch(f"/api/research/experiments/{experiment['id']}", json={
            "expected_version": current["version"], "status": "succeeded",
        }, headers=headers)
        assert reviewed.status_code == 200, reviewed.text
        assert reviewed.json()["data"]["result_source_refs"][0]["version"] == 2
        assert reviewed.json()["data"]["review_reason"] == ""
        baseline_review = client.post(f"/api/research/documents/{baseline['id']}/review", json={
            "expected_version": 1, "approved": True,
        }, headers=headers)
        assert baseline_review.status_code == 200, baseline_review.text
        resumed = client.get(f"/api/research/projects/{item['id']}/state", headers=headers).json()["data"]
        assert resumed["baseline"]["review_required"] is False


def test_baseline_own_withdrawal_keeps_pointer_and_requires_review(client, auth):
    headers = auth("baseline-own-withdrawal-user")
    item = project(client, headers)
    baseline = client.post(f"/api/research/projects/{item['id']}/study-notes", json={
        "title": "当前基线", "content": "用户基线内容", "document_type": "baseline", "confidentiality": "L2",
    }, headers=headers).json()["data"]
    approved = client.post(f"/api/research/documents/{baseline['id']}/review", json={
        "expected_version": 1, "approved": True,
    }, headers=headers)
    assert approved.status_code == 200, approved.text
    selected = client.post(f"/api/research/projects/{item['id']}/baseline", json={
        "document_id": baseline["id"],
    }, headers=headers)
    assert selected.status_code == 200, selected.text
    withdrawn = client.post(f"/api/research/documents/{baseline['id']}/review", json={
        "expected_version": 1, "approved": False,
    }, headers=headers)
    assert withdrawn.status_code == 200, withdrawn.text
    state = client.get(f"/api/research/projects/{item['id']}/state", headers=headers).json()["data"]
    assert state["baseline"]["id"] == baseline["id"]
    assert state["baseline"]["review_required"] is True
    assert state["baseline"]["status"] == "rejected"
