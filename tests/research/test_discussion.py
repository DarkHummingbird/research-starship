from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from app.gateway.config import ModelSpec


def _configure(monkeypatch):
    spec = ModelSpec(
        id="deepseek-chat", model="deepseek-chat", vendor="deepseek",
        base_url="https://api.deepseek.test/v1", api_key_env=("TEST_DISCUSSION_KEY",),
    )
    monkeypatch.setenv("TEST_DISCUSSION_KEY", "synthetic")
    monkeypatch.setattr("app.gateway.config.get_config", lambda: SimpleNamespace(models={spec.id: spec}))
    calls = Mock(return_value=SimpleNamespace(content="讨论回答"))
    monkeypatch.setattr("app.gateway.providers.call", calls)
    return calls


def _project(client, headers):
    return client.post("/api/research/projects", json={"name": "自由讨论"}, headers=headers).json()["data"]


def test_discussion_answers_without_reading_documents(client, auth, monkeypatch):
    calls = _configure(monkeypatch)
    headers = auth("discussion-user")
    project = _project(client, headers)
    response = client.post(
        f"/api/research/projects/{project['id']}/discussion",
        json={"message": "帮我拆解研究问题", "model_id": "deepseek-chat", "query_confidentiality": "L1"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["answer"] == "讨论回答"
    assert data["model"] == "external:deepseek-chat"
    assert calls.call_count == 1
    assert "不得编造来源" in calls.call_args.kwargs["system"]


def test_discussion_l3_cannot_use_external_model(client, auth, monkeypatch):
    _configure(monkeypatch)
    headers = auth("discussion-l3-user")
    project = _project(client, headers)
    response = client.post(
        f"/api/research/projects/{project['id']}/discussion",
        json={"message": "保密讨论", "model_id": "deepseek-chat", "query_confidentiality": "L3"},
        headers=headers,
    )
    assert response.status_code == 403, response.text


def test_writing_kind_is_available_in_generation_and_ui():
    from app.research.generations import KINDS

    assert "writing" in KINDS
    html = (Path(__file__).resolve().parents[2] / "static/research.html").read_text(encoding="utf-8")
    assert 'value="writing"' in html
    assert 'data-view-target="discussion"' in html

def test_discussion_includes_work_profile_as_background(client, auth, monkeypatch):
    from app.data_access import user_scope
    from app.db import save_profile

    calls = _configure(monkeypatch)
    uid = "discussion-profile-user"
    with user_scope(uid):
        save_profile(uid, {"name": "贾东霖", "interests": ["混合检索"]})
    headers = auth(uid)
    project = _project(client, headers)
    response = client.post(
        f"/api/research/projects/{project['id']}/discussion",
        json={"message": "帮我拆问题", "model_id": "deepseek-chat", "query_confidentiality": "L1"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    prompt = calls.call_args.kwargs["user"]
    assert "贾东霖" in prompt and "混合检索" in prompt
    assert "仅作背景，不是证据" in prompt


def test_discussion_history_persisted_and_separate_from_chat(client, auth, monkeypatch):
    _configure(monkeypatch)
    headers = auth("discussion-history-user")
    project = _project(client, headers)
    response = client.post(
        f"/api/research/projects/{project['id']}/discussion",
        json={"message": "帮我拆问题", "model_id": "deepseek-chat", "query_confidentiality": "L1"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    conversation_id = response.json()["data"]["conversation_id"]
    messages = client.get(f"/api/research/conversations/{conversation_id}/messages", headers=headers).json()["data"]["items"]
    assert [item["role"] for item in messages] == ["user", "assistant"]
    conversations = client.get(f"/api/research/projects/{project['id']}/conversations", headers=headers).json()["data"]["items"]
    assert all(not item["title"].startswith("自由讨论：") for item in conversations)
    discussions = client.get(f"/api/research/projects/{project['id']}/discussions", headers=headers).json()["data"]["items"]
    assert len(discussions) == 1
