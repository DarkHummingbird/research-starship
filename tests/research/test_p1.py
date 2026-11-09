from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.research import gateway, service, store
from app.research.parser import ExtractionValidationError, validate_document_card, validate_p0_model_result


P0_ROOT = Path(__file__).resolve().parents[3]
TUTORIAL = (P0_ROOT / "测试样本" / "虚构Agent使用教程.md").read_bytes()


@pytest.fixture
def research_dir(tmp_path, monkeypatch):
    path = tmp_path / "research-data"
    monkeypatch.setenv("RESEARCH_DATA_DIR", str(path))
    return path


@pytest.fixture
def user(auth):
    from app.data_access import user_scope

    uid = "research-p1-user"
    with user_scope(uid):
        yield uid, auth(uid)


def create_project(client, user, name):
    uid, headers = user
    response = client.post("/api/research/projects", json={"name": name, "goal": "P1 验收"}, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["data"]


def import_tutorial(client, user, project_id, level="L2"):
    _, headers = user
    response = client.post(
        f"/api/research/projects/{project_id}/documents",
        files={"file": ("虚构Agent使用教程.md", TUTORIAL, "text/markdown")},
        data={"confidentiality": level},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


def review(client, user, document, approved=True):
    _, headers = user
    response = client.post(
        f"/api/research/documents/{document['id']}/review",
        json={"expected_version": document["version"], "approved": approved},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


def ask(client, user, project_id, question, **extra):
    _, headers = user
    response = client.post(
        f"/api/research/projects/{project_id}/chat",
        json={"message": question, "provider": "local", **extra},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_tutorial_review_new_conversation_sources_and_project_isolation(client, user, research_dir, tmp_path):
    first = create_project(client, user, "课题甲")
    second = create_project(client, user, "课题乙")
    original = tmp_path / "original.md"
    original.write_bytes(TUTORIAL)
    before = hashlib.sha256(original.read_bytes()).hexdigest()

    document = import_tutorial(client, user, first["id"])
    assert document["status"] == "pending"
    assert document["duplicate_of"] is None if "duplicate_of" in document else True
    assert hashlib.sha256(original.read_bytes()).hexdigest() == before
    uid, headers = user
    detail = client.get(f"/api/research/documents/{document['id']}", headers=headers).json()["data"]
    assert detail["system_copy_matches_original"] is True
    assert len(detail["sources"]) == 6
    assert client.get(f"/api/research/projects/{first['id']}/memories", headers=headers).json()["data"]["items"] == []

    before_review = ask(client, user, first["id"], "资料什么时候进入正式资料库？")
    assert "未找到可核验证据" in before_review["answer"]
    approved = review(client, user, detail)
    assert approved["status"] == "approved"
    repeated = review(client, user, detail)
    assert repeated["idempotent"] is True
    memories = client.get(f"/api/research/projects/{first['id']}/memories", headers=headers).json()["data"]["items"]
    assert len(memories) == 6

    answered = ask(client, user, first["id"], "资料什么时候进入正式资料库？")
    assert answered["conversation_id"] != before_review["conversation_id"]
    assert "确认入库" in answered["answer"]
    assert answered["sources"][0]["chapter"] == "2. 导入与确认"
    assert answered["model"] == "local-extractive-v1"
    messages = client.get(
        f"/api/research/conversations/{answered['conversation_id']}/messages", headers=headers
    ).json()["data"]["items"]
    assert [message["role"] for message in messages] == ["user", "assistant"]
    assert messages[1]["sources"][0]["chunk_id"].startswith(document["id"])

    isolated = ask(client, user, second["id"], "资料什么时候进入正式资料库？")
    assert isolated["sources"] == []
    assert "未找到可核验证据" in isolated["answer"]
    cross = ask(client, user, second["id"], "资料什么时候进入正式资料库？", cross_project_ids=[first["id"]])
    assert cross["sources"][0]["project_id"] == first["id"]
    no_evidence = ask(client, user, first["id"], "该教程中的系统支持多少个课题？")
    assert no_evidence["sources"] == []
    assert "未找到可核验证据" in no_evidence["answer"]


def test_tutorial_fixed_questions_hit_expected_chapters(client, user, research_dir):
    project = create_project(client, user, "固定问题")
    document = import_tutorial(client, user, project["id"])
    review(client, user, document)
    cases = [
        ("资料什么时候进入正式资料库？", "2. 导入与确认"),
        ("默认搜索哪个课题？", "3. 搜索与提问"),
        ("回收站到期后是否自动清空？", "4. 删除与恢复"),
        ("保密资料能发给外部模型吗？", "5. 内部资料外发"),
    ]
    for question, chapter in cases:
        result = ask(client, user, project["id"], question)
        assert result["sources"], question
        assert len(result["sources"]) == 1
        assert result["sources"][0]["chapter"] == chapter


def test_l2_external_chat_requires_approval_and_sends_once(client, user, research_dir, monkeypatch):
    project = create_project(client, user, "L2 审批")
    document = import_tutorial(client, user, project["id"], "L2")
    review(client, user, document)
    monkeypatch.setattr(service, "_model_destination", lambda model_id: ("deepseek", "https://api.deepseek.test/v1"))
    calls = []
    monkeypatch.setattr(gateway, "configured_model_sender", lambda approval: calls.append(approval) or "受控模拟回答")

    pending = ask(client, user, project["id"], "默认搜索哪个课题？", provider="external", model_id="deepseek-test")
    assert pending["status"] == "awaiting_outbound_approval"
    assert calls == []
    approval = pending["approval"]
    assert approval["provider"] == "deepseek"
    assert approval["destination"] == "https://api.deepseek.test/v1"
    assert approval["purpose"] == "科研课题问答"
    assert approval["scope"]["fragments"]
    assert all(item["version"] == document["version"] or item["chunk_id"] == "query" for item in approval["scope"]["fragments"])

    _, headers = user
    decided = client.post(
        f"/api/research/outbound/{approval['id']}/decision", json={"approved": True}, headers=headers
    )
    assert decided.status_code == 200, decided.text
    sent = ask(
        client, user, project["id"], "默认搜索哪个课题？", provider="external", model_id="deepseek-test",
        conversation_id=pending["conversation_id"], task_id=pending["task_id"], approval_id=approval["id"],
    )
    assert sent["status"] == "answered"
    assert len(calls) == 1
    with pytest.raises(gateway.PolicyError) as repeated:
        gateway.execute_once(user[0], approval["id"], lambda _approval: calls.append(_approval) or "duplicate")
    assert repeated.value.code == "outbound_not_approved"
    assert len(calls) == 1
    events = client.get(f"/api/research/projects/{project['id']}/security-events", headers=headers).json()["data"]["items"]
    assert [event["outcome"] for event in events[:2]] == ["sent", "approval_required"]


def test_l2_changed_material_invalidates_approval_without_send(client, user, research_dir):
    project = create_project(client, user, "失效审批")
    document = import_tutorial(client, user, project["id"], "L2")
    source = document["parsed"]["entries"][0]["evidence"]
    content = document["parsed"]["entries"][0]["statement"]
    fragment = {
        "document_id": document["id"], "version": 1, "chunk_id": source["chunk_id"],
        "chapter": source["chapter"], "snippet": content, "classification": "L2",
        "sha256": hashlib.sha256(content.encode()).hexdigest(),
    }
    approval = gateway.prepare(
        user[0], project["id"], task_id="task-changing", provider="deepseek", model_id="deepseek-test",
        destination="https://api.deepseek.test/v1", purpose="摘要", operation="summary", fragments=[fragment],
    )
    gateway.decide(user[0], approval["id"], True)
    updated = service.update_document(user[0], document["id"], expected_version=1, title="教程（新版本）")
    assert updated["version"] == 2 and updated["status"] == "pending"
    calls = []
    with pytest.raises(gateway.PolicyError) as blocked:
        gateway.execute_once(user[0], approval["id"], lambda item: calls.append(item) or "should not send")
    assert blocked.value.code == "approval_scope_changed"
    assert calls == []
    assert gateway.get(user[0], approval["id"])["status"] == "invalidated"


def test_l2_changed_destination_requires_new_approval(client, user, research_dir):
    project = create_project(client, user, "范围变化")
    fragment = {
        "document_id": None, "version": 0, "chunk_id": "query", "chapter": "用户问题",
        "snippet": "虚构内部检索词", "classification": "L2", "sha256": hashlib.sha256(b"query").hexdigest(),
    }
    first = gateway.prepare(
        user[0], project["id"], task_id="same-task", provider="deepseek", model_id="one",
        destination="https://one.test/v1", purpose="问答", operation="qa", fragments=[fragment],
    )
    gateway.decide(user[0], first["id"], True)
    second = gateway.prepare(
        user[0], project["id"], task_id="same-task", provider="qwen", model_id="two",
        destination="https://two.test/v1", purpose="问答", operation="qa", fragments=[fragment],
    )
    assert second["id"] != first["id"] and second["status"] == "pending"
    assert gateway.get(user[0], first["id"])["status"] == "invalidated"
    with pytest.raises(gateway.PolicyError):
        gateway.execute_once(user[0], first["id"], lambda _item: "not allowed")


def test_l3_local_flow_works_and_every_external_operation_is_blocked(client, user, research_dir):
    project = create_project(client, user, "L3 禁外发")
    document = import_tutorial(client, user, project["id"], "L3")
    review(client, user, document)
    answered = ask(client, user, project["id"], "保密资料能发给外部模型吗？")
    assert answered["model"] == "local-extractive-v1"
    assert answered["confidentiality"] == "L3"
    source = answered["sources"][0]
    fragment = {
        "document_id": source["document_id"], "version": source["version"], "chunk_id": source["chunk_id"],
        "chapter": source["chapter"], "snippet": source["quote"], "classification": "L3",
        "sha256": hashlib.sha256(source["quote"].encode()).hexdigest(),
    }
    for operation in ("qa", "summary", "embedding", "ocr", "search", "export"):
        with pytest.raises(gateway.PolicyError) as blocked:
            gateway.prepare(
                user[0], project["id"], task_id=f"l3-{operation}", provider="deepseek", model_id="external",
                destination="https://external.test", purpose=operation, operation=operation, fragments=[fragment],
            )
        assert blocked.value.code == "l3_external_forbidden"
    _, headers = user
    events = client.get(f"/api/research/projects/{project['id']}/security-events", headers=headers).json()["data"]["items"]
    assert len([event for event in events if event["outcome"] == "blocked_l3"]) == 6


def test_private_timeout_is_not_retried(client, user, research_dir):
    project = create_project(client, user, "失败不重试")
    fragment = {
        "document_id": None, "version": 0, "chunk_id": "query", "chapter": "用户问题",
        "snippet": "虚构内部内容", "classification": "L2", "sha256": hashlib.sha256(b"private").hexdigest(),
    }
    approval = gateway.prepare(
        user[0], project["id"], task_id="timeout-task", provider="deepseek", model_id="external",
        destination="https://external.test", purpose="问答", operation="qa", fragments=[fragment],
    )
    gateway.decide(user[0], approval["id"], True)
    calls = []

    def timeout(_approval):
        calls.append(1)
        raise TimeoutError("synthetic timeout")

    with pytest.raises(gateway.PolicyError) as failed:
        gateway.execute_once(user[0], approval["id"], timeout)
    assert failed.value.code == "external_call_failed_no_retry"
    assert len(calls) == 1
    with pytest.raises(gateway.PolicyError):
        gateway.execute_once(user[0], approval["id"], timeout)
    assert len(calls) == 1


def test_p0_schema_type_and_source_regressions():
    first = json.loads((P0_ROOT / "首次-离线结构化结果.json").read_text())["data"]
    second = json.loads((P0_ROOT / "离线结构化结果.json").read_text())["data"]
    with pytest.raises(ExtractionValidationError):
        validate_p0_model_result(first, {1, 2, 3, 4, 5})
    with pytest.raises(ExtractionValidationError) as wrong_type:
        validate_p0_model_result(second, {1, 2, 3, 4, 5})
    assert "retention_days" in str(wrong_type.value)
    valid = json.loads(json.dumps(second))
    valid["retention_days"] = 30
    validate_p0_model_result(valid, {1, 2, 3, 4, 5})
    valid["entry"]["source"]["chapter"] = 99
    with pytest.raises(ExtractionValidationError) as wrong_source:
        validate_p0_model_result(valid, {1, 2, 3, 4, 5})
    assert "来源章节不存在" in str(wrong_source.value)


def test_document_source_tampering_and_cross_user_access_are_rejected(client, user, auth, research_dir):
    project = create_project(client, user, "来源校验")
    document = import_tutorial(client, user, project["id"])
    parsed = document["parsed"]
    chunks = [{
        "id": entry["evidence"]["chunk_id"], "document_id": document["id"], "document_version": 1,
        "chapter": entry["evidence"]["chapter"], "content": entry["statement"],
    } for entry in parsed["entries"]]
    parsed["entries"][0]["evidence"]["chapter"] = "伪造章节"
    with pytest.raises(ExtractionValidationError):
        validate_document_card(parsed, chunks)
    other_headers = auth("another-research-user")
    response = client.get(f"/api/research/documents/{document['id']}", headers=other_headers)
    assert response.status_code == 404


def test_l1_external_chat_needs_no_privacy_approval_but_sends_only_once(client, user, research_dir, monkeypatch):
    project = create_project(client, user, "L1 外发")
    document = import_tutorial(client, user, project["id"], "L1")
    review(client, user, document)
    monkeypatch.setattr(service, "_model_destination", lambda model_id: ("deepseek", "https://api.deepseek.test/v1"))
    calls = []
    monkeypatch.setattr(gateway, "configured_model_sender", lambda approval: calls.append(approval) or "公开资料回答")
    result = ask(client, user, project["id"], "默认搜索哪个课题？", provider="external", model_id="public-model")
    assert result["status"] == "answered"
    assert len(calls) == 1
    assert calls[0]["confidentiality"] == "L1"
    assert calls[0]["status"] == "approved"


def test_research_requests_bypass_external_observability(client, user, research_dir, monkeypatch):
    from app.observability import tracing

    def fail_if_exporting(*_args, **_kwargs):
        raise AssertionError("research request entered external tracing")

    monkeypatch.setattr(tracing, "request_trace", fail_if_exporting)
    _, headers = user
    response = client.get("/api/research/capabilities", headers=headers)
    assert response.status_code == 200
    capabilities = response.json()["data"]
    assert capabilities["automatic_memory_write"] is False
    assert capabilities["automatic_summary"] is False
    assert capabilities["automatic_model_fallback"] is False
    assert capabilities["local"]["ocr"]["mode"] == "offline_subprocess"
    assert capabilities["local"]["embedding"]["mode"] == "offline_subprocess"
    assert capabilities["local"]["rerank"]["available"] is False


def test_rejected_document_never_enters_memory(client, user, research_dir):
    project = create_project(client, user, "拒绝审核")
    document = import_tutorial(client, user, project["id"])
    rejected = review(client, user, document, approved=False)
    assert rejected["status"] == "rejected"
    repeated = review(client, user, document, approved=False)
    assert repeated["idempotent"] is True
    _, headers = user
    memories = client.get(f"/api/research/projects/{project['id']}/memories", headers=headers)
    assert memories.status_code == 200
    assert memories.json()["data"]["items"] == []
    result = ask(client, user, project["id"], "默认搜索哪个课题？")
    assert result["sources"] == []
    assert "未找到可核验证据" in result["answer"]
