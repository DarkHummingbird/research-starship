from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, func, select

from app.data_access import user_scope
from app.data_access.research import memory_session, migration_session
from app.research import store
from tests.research.test_research_state import approved_document, project


def set_analysis(user_id, document, field, statement, comparison=None):
    chunk = document["sources"][0]
    with user_scope(user_id), memory_session() as conn:
        row = conn.get(store.ResearchDocument, document["id"])
        analysis = store.json_value(row.analysis_json, {})
        analysis[field] = [{
            "statement": statement,
            "evidence": {"chunk_id": chunk["id"], "quote": chunk["content"]},
        }]
        if field == "metrics":
            analysis[field][0]["value"] = "87.4"
            analysis[field][0]["unit"] = "%"
        if comparison:
            analysis[field][0]["comparison"] = comparison
        row.analysis_json = json.dumps(analysis, ensure_ascii=False)
        conn.flush()


def test_structured_synthesis_comparability_and_note(client, auth, monkeypatch):
    uid = "synthesis-user"
    headers = auth(uid)
    item = project(client, headers)
    first = approved_document(client, headers, item["id"], monkeypatch, title="Sparse retrieval")
    second = approved_document(client, headers, item["id"], monkeypatch, title="Dense retrieval")
    set_analysis(uid, first, "datasets", "SCIDOCS is not used; fixed local public examples")
    set_analysis(uid, second, "datasets", "QASPER research questions")
    set_analysis(uid, first, "metrics", "Sparse retrieval accuracy")
    set_analysis(uid, second, "metrics", "Dense retrieval accuracy")
    response = client.post(f"/api/research/projects/{item['id']}/synthesis", json={
        "document_ids": [first["id"], second["id"]], "query": "比较检索证据", "provider": "structured",
    }, headers=headers)
    assert response.status_code == 200, response.text
    result = response.json()["data"]
    assert len(result["documents"]) == 2
    assert result["experiments_executed"] is False
    assert "指标只能在条件核验后比较" in result["comparability"][0]
    assert result["generation_id"]
    saved = client.post(f"/api/research/synthesis/{result['generation_id']}/save-note", json={"title": "检索方法比较"}, headers=headers)
    assert saved.status_code == 201, saved.text
    note = saved.json()["data"]
    assert note["status"] == "pending"
    assert note["parsed"]["note_type"] == "ai_summary"
    assert note["parsed"]["note_provenance"]
    reviewed = client.post(f"/api/research/documents/{note['id']}/review", json={
        "expected_version": note["version"], "approved": True,
    }, headers=headers)
    assert reviewed.status_code == 200, reviewed.text
    duplicate = client.post(f"/api/research/synthesis/{result['generation_id']}/save-note", json={"title": "重复"}, headers=headers)
    assert duplicate.status_code == 409


def test_synthesis_rejects_pending_and_foreign_documents(client, auth, monkeypatch):
    uid = "synthesis-private-user"
    headers = auth(uid)
    item = project(client, headers)
    first = approved_document(client, headers, item["id"], monkeypatch)
    second = approved_document(client, headers, item["id"], monkeypatch)
    other = auth("synthesis-other-user")
    assert client.post(f"/api/research/projects/{item['id']}/synthesis", json={
        "document_ids": [first["id"], second["id"]], "provider": "structured",
    }, headers=other).status_code == 404
    from app.research.ingest import import_document
    with user_scope(uid):
        pending = import_document(uid, item["id"], filename="pending.md", content=b"# Pending\nNot approved", confidentiality="L2", document_type="tutorial")
    response = client.post(f"/api/research/projects/{item['id']}/synthesis", json={
        "document_ids": [first["id"], pending["id"]], "provider": "structured",
    }, headers=headers)
    assert response.status_code == 409


def test_online_synthesis_approval(client, auth, monkeypatch):
    uid = "synthesis-online-user"
    headers = auth(uid)
    item = project(client, headers, name="在线综合")
    first = approved_document(client, headers, item["id"], monkeypatch)
    second = approved_document(client, headers, item["id"], monkeypatch)
    from app.gateway.config import ModelSpec
    spec = ModelSpec(id="online", model="online", vendor="deepseek", base_url="https://api.deepseek.com/v1", api_key_env=("TEST_SYNTHESIS_KEY",))
    monkeypatch.setenv("TEST_SYNTHESIS_KEY", "synthetic")
    monkeypatch.setattr("app.gateway.config.get_config", lambda: SimpleNamespace(models={"online": spec}))
    calls = []
    monkeypatch.setattr("app.gateway.providers.call", lambda *args, **kwargs: calls.append(kwargs) or SimpleNamespace(content="条件不同，不直接比较。"))
    body = {"document_ids": [first["id"], second["id"]], "provider": "external", "model_id": "online"}
    pending = client.post(f"/api/research/projects/{item['id']}/synthesis", json=body, headers=headers)
    assert pending.status_code == 200, pending.text
    result = pending.json()["data"]
    assert result["status"] == "awaiting_outbound_approval"
    assert calls == []
    approval = result["approval"]
    approved = client.post(f"/api/research/outbound/{approval['id']}/decision", json={"approved": True}, headers=headers)
    assert approved.status_code == 200
    completed = client.post(f"/api/research/projects/{item['id']}/synthesis", json={
        **body, "task_id": result["task_id"], "approval_id": approval["id"],
    }, headers=headers)
    assert completed.status_code == 200, completed.text
    assert completed.json()["data"]["summary"] == "条件不同，不直接比较。"
    assert len(calls) == 1
    assert calls[0]["max_tokens"] == 1200



def test_unstructured_document_uses_current_chunks_within_budget(client, auth, monkeypatch):
    from app.research.ingest import import_document
    from app.research import ingest
    monkeypatch.setattr(ingest, "embed_texts", lambda texts: {
        "vectors": [[1.0] + [0.0] * 1023 for _ in texts], "model": "synthetic-online-embedding", "dimensions": 1024,
    })
    uid = "synthesis-chunks-user"
    headers = auth(uid)
    item = project(client, headers)
    with user_scope(uid):
        imported = import_document(uid, item["id"], filename="multi.md", content=b"# Multi\n\n## Method\n\nBM25 uses sparse terms.\n\n## Results\n\nThe correct source is returned.", confidentiality="L1", document_type="tutorial")
    approved = client.post(f"/api/research/documents/{imported['id']}/review", json={"expected_version": 1, "approved": True}, headers=headers)
    assert approved.status_code == 200, approved.text
    first = client.get(f"/api/research/documents/{imported['id']}", headers=headers).json()["data"]
    second = approved_document(client, headers, item["id"], monkeypatch, title="Other")
    result = client.post(f"/api/research/projects/{item['id']}/synthesis", json={
        "document_ids": [first["id"], second["id"]], "provider": "structured",
    }, headers=headers).json()["data"]
    row = next(item for item in result["documents"] if item["document_id"] == first["id"])
    assert len(row["facts"]) <= len(first["sources"])
    assert "The correct source is returned." in result["content"]


def _configure_online(monkeypatch):
    from app.gateway.config import ModelSpec

    spec = ModelSpec(
        id="online", model="online", vendor="deepseek", base_url="https://api.deepseek.com/v1",
        api_key_env=("TEST_SYNTHESIS_KEY",),
    )
    monkeypatch.setenv("TEST_SYNTHESIS_KEY", "synthetic")
    monkeypatch.setattr("app.gateway.config.get_config", lambda: SimpleNamespace(models={"online": spec}))
    return spec


def _approve_online(client, headers, project_id, body):
    pending = client.post(f"/api/research/projects/{project_id}/synthesis", json=body, headers=headers)
    assert pending.status_code == 200, pending.text
    result = pending.json()["data"]
    assert result["status"] == "awaiting_outbound_approval"
    approval = result["approval"]
    decided = client.post(
        f"/api/research/outbound/{approval['id']}/decision", json={"approved": True}, headers=headers,
    )
    assert decided.status_code == 200, decided.text
    return result, approval


def test_concurrent_synthesis_save_creates_only_one_note(client, auth, monkeypatch):
    uid = "synthesis-concurrent-user"
    headers = auth(uid)
    item = project(client, headers)
    first = approved_document(client, headers, item["id"], monkeypatch, title="Concurrent A")
    second = approved_document(client, headers, item["id"], monkeypatch, title="Concurrent B")
    generated = client.post(f"/api/research/projects/{item['id']}/synthesis", json={
        "document_ids": [first["id"], second["id"]], "provider": "structured",
    }, headers=headers).json()["data"]

    def save(title):
        return client.post(
            f"/api/research/synthesis/{generated['generation_id']}/save-note",
            json={"title": title}, headers=headers,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(save, ("并发保存 A", "并发保存 B")))
    assert sorted(response.status_code for response in responses) == [201, 409]
    with user_scope(uid), migration_session() as conn:
        notes = conn.scalars(select(store.ResearchDocument).where(
            store.ResearchDocument.user_id == uid,
            store.ResearchDocument.project_id == item["id"],
            store.ResearchDocument.document_type == "research_note",
        )).all()
        generation = conn.get(store.ResearchGeneration, generated["generation_id"])
    assert len(notes) == 1
    assert generation.status == "archived"
    assert generation.archived_document_id == notes[0].id


@pytest.mark.parametrize("change", ["update", "withdraw", "delete"])
def test_online_synthesis_rechecks_sources_after_model_returns(client, auth, monkeypatch, change):
    uid = f"synthesis-source-{change}-user"
    headers = auth(uid)
    item = project(client, headers)
    first = approved_document(client, headers, item["id"], monkeypatch, title="Changing source")
    second = approved_document(client, headers, item["id"], monkeypatch, title="Stable source")
    _configure_online(monkeypatch)
    calls = []

    def change_source_during_call(*args, **kwargs):
        calls.append(kwargs)
        if change == "update":
            changed = client.patch(f"/api/research/documents/{first['id']}", json={
                "expected_version": first["version"], "title": "Updated during synthesis",
            }, headers=headers)
        elif change == "withdraw":
            changed = client.post(f"/api/research/documents/{first['id']}/review", json={
                "expected_version": first["version"], "approved": False,
            }, headers=headers)
        else:
            changed = client.post(f"/api/research/documents/{first['id']}/delete", json={
                "expected_version": first["version"], "confirmed": True,
            }, headers=headers)
        assert changed.status_code == 200, changed.text
        return SimpleNamespace(content="这份旧综合不能保存")

    monkeypatch.setattr("app.gateway.providers.call", change_source_during_call)
    monkeypatch.setattr("app.research.generations.generate", lambda *args, **kwargs: pytest.fail("不得切换本地模型"))
    body = {"document_ids": [first["id"], second["id"]], "provider": "external", "model_id": "online"}
    pending, approval = _approve_online(client, headers, item["id"], body)
    completed = client.post(f"/api/research/projects/{item['id']}/synthesis", json={
        **body, "task_id": pending["task_id"], "approval_id": approval["id"],
    }, headers=headers)
    assert completed.status_code == 409, completed.text
    assert completed.json()["detail"]["code"] == "synthesis_source_changed"
    assert completed.json()["detail"]["message"] == "资料已变化，请重新生成"
    assert len(calls) == 1
    with user_scope(uid), migration_session() as conn:
        count = conn.scalar(select(func.count(store.ResearchGeneration.id)).where(
            store.ResearchGeneration.user_id == uid,
        ))
    assert count == 0


def test_synthesis_note_save_rechecks_source_snapshot(client, auth, monkeypatch):
    uid = "synthesis-save-source-user"
    headers = auth(uid)
    item = project(client, headers)
    first = approved_document(client, headers, item["id"], monkeypatch)
    second = approved_document(client, headers, item["id"], monkeypatch, title="Other")
    generated = client.post(f"/api/research/projects/{item['id']}/synthesis", json={
        "document_ids": [first["id"], second["id"]], "provider": "structured",
    }, headers=headers).json()["data"]
    changed = client.patch(f"/api/research/documents/{first['id']}", json={
        "expected_version": first["version"], "title": "Changed before save",
    }, headers=headers)
    assert changed.status_code == 200, changed.text
    saved = client.post(
        f"/api/research/synthesis/{generated['generation_id']}/save-note", json={"title": "旧综合"}, headers=headers,
    )
    assert saved.status_code == 409
    assert saved.json()["detail"]["code"] == "synthesis_source_changed"
    with user_scope(uid), migration_session() as conn:
        count = conn.scalar(select(func.count(store.ResearchDocument.id)).where(
            store.ResearchDocument.user_id == uid,
            store.ResearchDocument.document_type == "research_note",
        ))
    assert count == 0


def test_online_synthesis_failure_is_not_retried_or_sent_to_local_model(client, auth, monkeypatch):
    uid = "synthesis-online-failure-user"
    headers = auth(uid)
    item = project(client, headers)
    first = approved_document(client, headers, item["id"], monkeypatch)
    second = approved_document(client, headers, item["id"], monkeypatch, title="Other")
    _configure_online(monkeypatch)
    calls = []

    def fail_once(*args, **kwargs):
        calls.append(kwargs)
        raise RuntimeError("synthetic online outage")

    monkeypatch.setattr("app.gateway.providers.call", fail_once)
    monkeypatch.setattr("app.research.generations.generate", lambda *args, **kwargs: pytest.fail("不得切换本地模型"))
    body = {"document_ids": [first["id"], second["id"]], "provider": "external", "model_id": "online"}
    pending, approval = _approve_online(client, headers, item["id"], body)
    failed = client.post(f"/api/research/projects/{item['id']}/synthesis", json={
        **body, "task_id": pending["task_id"], "approval_id": approval["id"],
    }, headers=headers)
    assert failed.status_code == 502, failed.text
    assert failed.json()["detail"]["code"] == "external_call_failed_no_retry"
    failed_approval = client.get(f"/api/research/outbound/{approval['id']}", headers=headers)
    assert failed_approval.json()["data"]["status"] == "failed", failed_approval.text
    repeated = client.post(f"/api/research/projects/{item['id']}/synthesis", json={
        **body, "task_id": pending["task_id"], "approval_id": approval["id"],
    }, headers=headers)
    if repeated.status_code == 200:
        retry = repeated.json()["data"]
        assert retry["status"] == "awaiting_outbound_approval", repeated.text
        assert retry["approval"]["id"] != approval["id"]
        assert retry["approval"]["status"] == "pending"
    else:
        assert repeated.status_code in {409, 502}, repeated.text
    assert len(calls) == 1


def _long_document(client, headers, uid, project_id, monkeypatch, index):
    from app.research import ingest

    monkeypatch.setattr(ingest, "embed_texts", lambda texts: {
        "vectors": [[1.0] + [0.0] * 1023 for _ in texts],
        "model": "synthetic-online-embedding", "dimensions": 1024,
    })
    sections = []
    for chapter in range(10):
        sections.append(f"## Chapter {chapter}\n\nMethod {chapter} " + (f"evidence-{chapter} " * 400))
    content = (f"# Long document {index}\n\n" + "\n\n".join(sections)).encode()
    with user_scope(uid):
        imported = ingest.import_document(
            uid, project_id, filename=f"long-{index}.md", content=content,
            confidentiality="L2", document_type="tutorial",
        )
    reviewed = client.post(f"/api/research/documents/{imported['id']}/review", json={
        "expected_version": 1, "approved": True,
    }, headers=headers)
    assert reviewed.status_code == 200, reviewed.text
    return client.get(f"/api/research/documents/{imported['id']}", headers=headers).json()["data"]


def test_long_multi_document_synthesis_has_deterministic_bounded_coverage(client, auth, monkeypatch):
    from app.research import synthesis

    uid = "synthesis-long-documents-user"
    headers = auth(uid)
    item = project(client, headers)
    documents = [_long_document(client, headers, uid, item["id"], monkeypatch, index) for index in range(5)]
    body = {"document_ids": [document["id"] for document in documents], "provider": "structured"}
    first = client.post(f"/api/research/projects/{item['id']}/synthesis", json=body, headers=headers)
    assert first.status_code == 200, first.text
    result = first.json()["data"]
    repeated = client.post(f"/api/research/projects/{item['id']}/synthesis", json=body, headers=headers)
    assert repeated.status_code == 200, repeated.text
    assert result["content"] == repeated.json()["data"]["content"]
    assert result["evidence_budget"]["truncated"] is True
    assert result["evidence_budget"]["selected_characters"] <= synthesis.MAX_TOTAL_CHARACTERS
    for row in result["documents"]:
        evidence = row["evidence"]
        assert evidence["selected_facts"] > 0
        assert evidence["selected_facts"] < evidence["available_facts"]
        assert evidence["selected_characters"] <= synthesis.MAX_DOCUMENT_CHARACTERS
        assert len(evidence["chapters"]) >= 2
        assert all(len(fact["quote"]) <= synthesis.MAX_FACT_CHARACTERS for fact in row["facts"])
    assert "未选内容未被模型读取" in result["content"]


def test_long_online_approval_shows_only_bounded_actual_scope(client, auth, monkeypatch):
    from app.research import synthesis

    uid = "synthesis-long-online-user"
    headers = auth(uid)
    item = project(client, headers)
    documents = [_long_document(client, headers, uid, item["id"], monkeypatch, index) for index in range(2)]
    _configure_online(monkeypatch)
    calls = []
    monkeypatch.setattr("app.gateway.providers.call", lambda *args, **kwargs: calls.append(kwargs))
    body = {
        "document_ids": [document["id"] for document in documents],
        "provider": "external", "model_id": "online", "query": "比较方法和实验条件",
    }
    result = client.post(f"/api/research/projects/{item['id']}/synthesis", json=body, headers=headers)
    assert result.status_code == 200, result.text
    data = result.json()["data"]
    assert data["status"] == "awaiting_outbound_approval"
    fragments = data["approval"]["scope"]["fragments"]
    assert len({fragment["document_id"] for fragment in fragments[1:]}) == 2
    assert sum(len(fragment["snippet"]) for fragment in fragments) <= synthesis.MAX_TOTAL_CHARACTERS + synthesis.MAX_QUERY_CHARACTERS
    assert all(len(fragment["snippet"]) <= synthesis.MAX_FACT_CHARACTERS for fragment in fragments[1:])
    assert calls == []


def test_synthesis_rejects_empty_evidence(client, auth, monkeypatch):
    uid = "synthesis-empty-evidence-user"
    headers = auth(uid)
    item = project(client, headers)
    first = approved_document(client, headers, item["id"], monkeypatch)
    second = approved_document(client, headers, item["id"], monkeypatch, title="Other")
    with user_scope(uid), memory_session() as conn:
        conn.execute(delete(store.ResearchChunk).where(store.ResearchChunk.document_id == first["id"]))
    response = client.post(f"/api/research/projects/{item['id']}/synthesis", json={
        "document_ids": [first["id"], second["id"]], "provider": "structured",
    }, headers=headers)
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "synthesis_evidence_empty"


def test_synthesis_includes_baseline_conditions_and_research_goal(client, auth, monkeypatch):
    uid = "synthesis-dimensions-user"
    headers = auth(uid)
    item = project(client, headers)
    first = approved_document(client, headers, item["id"], monkeypatch)
    second = approved_document(client, headers, item["id"], monkeypatch, title="Other")
    set_analysis(uid, first, "baseline", "Lexical baseline")
    set_analysis(uid, first, "metrics", "Hybrid accuracy", {
        "dataset": "SYNTH-LOCAL", "split": "fixed", "settings": "ten documents", "model_version": "v1",
    })
    set_analysis(uid, second, "metrics", "Dense accuracy", {
        "dataset": "SYNTH-LOCAL", "split": "random", "settings": "twenty documents", "model_version": "v2",
    })
    response = client.post(f"/api/research/projects/{item['id']}/synthesis", json={
        "document_ids": [first["id"], second["id"]], "provider": "structured",
        "query": "混合检索研究问题与实验条件是否一致？",
    }, headers=headers)
    assert response.status_code == 200, response.text
    result = response.json()["data"]
    assert "Lexical baseline" in result["content"]
    assert "实验设置：ten documents" in result["content"]
    assert "混合检索研究问题与实验条件是否一致？" in result["content"]
    assert any("实验设置、数据划分或模型版本" in note for note in result["comparability"])
