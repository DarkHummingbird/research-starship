from __future__ import annotations

import pytest
from sqlalchemy import select

from app.data_access.research import migration_session
from app.research import store
from app.research.conflicts import compare_facts


@pytest.fixture
def conflict_project(client, auth, tmp_path, monkeypatch):
    from app.data_access import user_scope

    monkeypatch.setenv("RESEARCH_DATA_DIR", str(tmp_path / "research"))
    uid = "p3-conflict-user"
    headers = auth(uid)
    with user_scope(uid):
        project = client.post("/api/research/projects", json={"name": "冲突处理"}, headers=headers)
        yield uid, headers, project.json()["data"]["id"]


def comparison():
    return {"dataset": "SYNTH-10", "split": "8:2", "metric": "accuracy", "unit": "%", "settings": "seed=42", "model_version": "SafeRAG-v1"}


def add_paper(client, headers, project_id, value, title):
    response = client.post(
        f"/api/research/projects/{project_id}/documents",
        files={"file": (f"{title}.md", f"# {title}\n\nSafeRAG accuracy is {value}% on SYNTH-10.".encode(), "text/markdown")},
        data={"document_type": "paper", "confidentiality": "L1"}, headers=headers,
    )
    assert response.status_code == 201, response.text
    document = response.json()["data"]
    fact = {
        "statement": f"SafeRAG accuracy is {value}%", "value": value, "unit": "%", "comparison": comparison(),
        "evidence": {"chunk_id": document["sources"][0]["id"], "quote": document["sources"][0]["content"]},
    }
    analysis = {
        "schema_version": "research-paper/v1", "record_type": "paper_statement",
        "method": [], "baseline": [], "datasets": [], "metrics": [fact], "limitations": [], "suspicious": [],
    }
    response = client.put(
        f"/api/research/documents/{document['id']}/analysis",
        json={"expected_version": 1, "analysis": analysis}, headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()["data"], fact


def review(client, headers, document):
    return client.post(
        f"/api/research/documents/{document['id']}/review",
        json={"expected_version": document["version"], "approved": True}, headers=headers,
    )


def test_same_condition_conflict_overwrite_and_other_resolutions(client, conflict_project):
    uid, headers, project_id = conflict_project
    original, _ = add_paper(client, headers, project_id, 80, "原始指标")
    assert review(client, headers, original).status_code == 200

    candidate, fact = add_paper(client, headers, project_id, 90, "同条件新指标")
    blocked = review(client, headers, candidate)
    assert blocked.status_code == 409, blocked.text
    detail = blocked.json()["detail"]
    conflict = detail["conflict"]
    assert detail["code"] == "metric_conflict_requires_resolution"
    assert conflict["comparability"] == "conflict"
    with migration_session() as conn:
        assert conn.execute(select(store.ResearchMemory).where(store.ResearchMemory.document_id == candidate["id"])).scalars().all() == []

    resolved = client.post(
        f"/api/research/conflicts/{conflict['id']}/resolve",
        json={"resolution": "overwrite"}, headers=headers,
    )
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["data"]["next_action"] == "review_candidate"
    assert review(client, headers, candidate).status_code == 200
    with migration_session() as conn:
        old = conn.execute(select(store.ResearchMemory).where(store.ResearchMemory.id == conflict["existing_memory_id"])).scalar_one()
        assert old.active is False
        new = conn.execute(select(store.ResearchMemory).where(store.ResearchMemory.document_id == candidate["id"], store.ResearchMemory.entry_id == "metrics-1")).scalar_one()
        assert new.active is True

    parallel, fact = add_paper(client, headers, project_id, 91, "并列保留")
    blocked = review(client, headers, parallel)
    assert blocked.status_code == 409
    conflict = blocked.json()["detail"]["conflict"]
    response = client.post(f"/api/research/conflicts/{conflict['id']}/resolve", json={"resolution": "keep_both"}, headers=headers)
    assert response.status_code == 200
    assert review(client, headers, parallel).status_code == 200

    edited, fact = add_paper(client, headers, project_id, 92, "人工修正")
    blocked = review(client, headers, edited)
    conflict = blocked.json()["detail"]["conflict"]
    edited_fact = dict(fact)
    edited_fact["statement"] = "人工核对后的 SafeRAG 指标"
    response = client.post(
        f"/api/research/conflicts/{conflict['id']}/resolve",
        json={"resolution": "edit", "edited_fact": edited_fact}, headers=headers,
    )
    assert response.status_code == 200, response.text
    assert review(client, headers, edited).status_code == 200

    discarded, _ = add_paper(client, headers, project_id, 93, "放弃新记录")
    blocked = review(client, headers, discarded)
    conflict = blocked.json()["detail"]["conflict"]
    response = client.post(f"/api/research/conflicts/{conflict['id']}/resolve", json={"resolution": "discard"}, headers=headers)
    assert response.status_code == 200
    with migration_session() as conn:
        row = conn.execute(select(store.ResearchDocument).where(store.ResearchDocument.id == discarded["id"])).scalar_one()
        assert row.status == "rejected"
        assert conn.execute(select(store.ResearchMemory).where(store.ResearchMemory.document_id == discarded["id"])).scalars().all() == []


def test_different_or_missing_conditions_are_not_false_conflicts():
    left = {"value": 80, "comparison": comparison()}
    different = {"value": 90, "comparison": {**comparison(), "split": "5:5"}}
    assert compare_facts(left, different)[0] == "parallel"
    missing = {"value": 90, "comparison": {**comparison(), "split": ""}}
    status, missing_fields, _ = compare_facts(left, missing)
    assert status == "comparability_unknown" and missing_fields == ["split"]
    assert compare_facts(left, dict(left))[0] == "consistent"
