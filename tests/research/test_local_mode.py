from __future__ import annotations

from unittest.mock import Mock

from app.data_access import user_scope
from app.research import gateway, local_components, local_mode, service


def _set_mode(user_id: str, enabled: bool) -> None:
    with user_scope(user_id):
        local_mode.set_enabled(user_id, enabled)


def _without_local_model(monkeypatch):
    status = local_components.component_status()
    status["generative_llm"]["available"] = False
    monkeypatch.setattr(local_components, "component_status", lambda: status)


def test_local_mode_defaults_off_and_persists_per_account(client, auth, monkeypatch):
    with user_scope("never-configured-local-mode-user"):
        assert local_mode.is_enabled("never-configured-local-mode-user") is False
    _without_local_model(monkeypatch)
    owner = "local-mode-owner"
    other = "local-mode-other"
    headers = auth(owner)
    other_headers = auth(other)
    _set_mode(owner, False)
    _set_mode(other, False)

    initial = client.get("/api/research/capabilities", headers=headers).json()["data"]["local_mode"]
    assert initial["enabled"] is False
    assert initial["l3_available"] is False
    assert initial["local_generation_available"] is False

    enabled = client.put("/api/research/local-mode", json={"enabled": True}, headers=headers)
    assert enabled.status_code == 200, enabled.text
    assert enabled.json()["data"]["enabled"] is True
    assert enabled.json()["data"]["l3_available"] is False
    restored = client.get("/api/research/capabilities", headers=headers).json()["data"]["local_mode"]
    assert restored["enabled"] is True
    assert client.get("/api/research/capabilities", headers=other_headers).json()["data"]["local_mode"]["enabled"] is False

    disabled = client.put("/api/research/local-mode", json={"enabled": False}, headers=headers)
    assert disabled.status_code == 200
    assert disabled.json()["data"]["enabled"] is False


def test_disabled_mode_blocks_l3_and_local_models_without_local_process(client, auth, monkeypatch):
    uid = "local-mode-disabled-user"
    headers = auth(uid)
    _set_mode(uid, False)
    local_process = Mock(side_effect=AssertionError("不得启动本地模型或组件"))
    monkeypatch.setattr(local_components, "_run_local", local_process)
    project = client.post("/api/research/projects", json={"name": "默认在线流程"}, headers=headers).json()["data"]

    blocked_requests = [
        client.post(
            f"/api/research/projects/{project['id']}/documents",
            files={"file": ("private.md", b"# private", "text/markdown")},
            data={"confidentiality": "L3", "document_type": "tutorial"}, headers=headers,
        ),
        client.post(
            f"/api/research/projects/{project['id']}/documents",
            files={"file": ("semantic.md", b"# public", "text/markdown")},
            data={"confidentiality": "L1", "document_type": "tutorial", "chunking_strategy": "semantic"}, headers=headers,
        ),
        client.post(
            f"/api/research/projects/{project['id']}/chat",
            json={"message": "private", "provider": "local", "query_confidentiality": "L3"}, headers=headers,
        ),
        client.post(
            f"/api/research/projects/{project['id']}/chat-tasks",
            json={"message": "public", "provider": "rag", "query_confidentiality": "L1"}, headers=headers,
        ),
        client.post(
            f"/api/research/projects/{project['id']}/generation-tasks",
            json={"kind": "ablation", "query": "public", "provider": "rag"}, headers=headers,
        ),
        client.post(
            "/api/attachments", files={"file": ("private.md", b"private", "text/markdown")},
            data={"confidentiality": "L3", "allow_outbound": "false"}, headers=headers,
        ),
    ]
    for response in blocked_requests:
        assert response.status_code == 403, response.text
        assert response.json()["detail"]["code"] == "local_mode_disabled"

    imported = client.post(
        f"/api/research/projects/{project['id']}/documents",
        files={"file": ("public.md", "# 教程\n\n## 规则\n默认只搜索当前课题，确认后入库。".encode(), "text/markdown")},
        data={"confidentiality": "L1", "document_type": "tutorial"}, headers=headers,
    )
    assert imported.status_code == 201, imported.text
    document = imported.json()["data"]
    reviewed = client.post(
        f"/api/research/documents/{document['id']}/review",
        json={"expected_version": document["version"], "approved": True}, headers=headers,
    )
    assert reviewed.status_code == 200, reviewed.text

    monkeypatch.setattr(service, "_model_destination", lambda _model_id: ("synthetic", "https://example.test/v1"))
    sender = Mock(return_value="公开资料模拟在线回答")
    monkeypatch.setattr(gateway, "configured_model_sender", sender)
    answered = client.post(
        f"/api/research/projects/{project['id']}/chat",
        json={"message": "默认搜索哪个课题？", "provider": "external", "model_id": "synthetic-online"},
        headers=headers,
    )
    assert answered.status_code == 200, answered.text
    assert answered.json()["data"]["status"] == "answered"
    sender.assert_called_once()
    local_process.assert_not_called()
