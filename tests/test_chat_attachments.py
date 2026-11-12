from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import fitz
import pytest

from app.agent import attachments
from app.gateway.config import ModelSpec


@pytest.fixture(autouse=True)
def no_embedding(monkeypatch):
    monkeypatch.setattr(attachments, "_embedding", lambda chunks: "")


def upload(client, headers, content=b"# Notes\n\nThe candidate limit is 100.", filename="notes.md", **data):
    response = client.post(
        "/api/attachments",
        files={"file": (filename, content, "application/octet-stream")},
        data={"confidentiality": "L3", "allow_outbound": "false", **data},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]["attachment"]


def deepseek_spec():
    return ModelSpec(
        id="deepseek-flash", model="deepseek-flash", vendor="deepseek",
        base_url="https://api.deepseek.com/v1", api_key_env=("TEST_ATTACHMENT_DEEPSEEK_KEY",),
    )


def image_bytes():
    document = fitz.open()
    page = document.new_page(width=320, height=100)
    page.insert_text((20, 55), "Hello attachment", fontsize=18)
    result = page.get_pixmap().tobytes("png")
    document.close()
    return result


def test_document_chat_binds_thread_skips_memory_and_deletes_content(client, auth, monkeypatch):
    headers = auth("attachment-owner")
    generated = Mock(return_value={"text": "根据[附件1]，候选上限是 100。 [end of text]", "model": "local", "network": "denied"})
    monkeypatch.setattr(attachments, "generate", generated)
    from app.agent.subgraphs import chat as chat_subgraph
    write_event = Mock()
    summarize = Mock()
    monkeypatch.setattr(chat_subgraph, "write_event", write_event)
    monkeypatch.setattr(chat_subgraph, "maybe_summarize_turn", summarize)

    item = upload(client, headers)
    assert item["status"] == "ready" and item["processing_mode"] == "local_text"
    assert item["scope"] == "conversation_only" and not item["research_memory"] and not item["personal_memory"]
    listed = client.get("/api/attachments", headers=headers)
    assert listed.status_code == 200
    assert item["id"] in {row["id"] for row in listed.json()["data"]["items"]}
    response = client.post("/api/chat", json={
        "user_id": "attachment-owner", "message": "候选上限是多少？",
        "attachment_ids": [item["id"]],
    }, headers=headers)
    assert response.status_code == 200, response.text
    assert "100" in response.json()["data"]["messages"][0]["content"]
    assert "end of text" not in response.json()["data"]["messages"][0]["content"]
    assert "100" in generated.call_args.args[0]
    write_event.assert_not_called()
    summarize.assert_not_called()

    other_thread = client.post("/api/chat", json={
        "user_id": "attachment-owner", "message": "再问一次", "attachment_ids": [item["id"]],
    }, headers=headers)
    assert other_thread.status_code == 403
    other_user = auth("attachment-other-user")
    assert other_user and client.get(f"/api/attachments/{item['id']}", headers=other_user).status_code == 404

    from app.data_access import user_scope
    from app.data_access.attachments import ChatAttachment
    from app.db import memory_session
    from sqlalchemy import select
    with user_scope("attachment-owner"), memory_session() as conn:
        stored = conn.execute(select(ChatAttachment).where(ChatAttachment.id == item["id"])).scalar_one()
        path = Path(stored.stored_path)
        assert path.exists()
    deleted = client.delete(f"/api/attachments/{item['id']}", headers=headers)
    assert deleted.status_code == 200
    assert item["id"] not in {row["id"] for row in client.get("/api/attachments", headers=headers).json()["data"]["items"]}
    assert not path.exists()
    with user_scope("attachment-owner"), memory_session() as conn:
        stored = conn.execute(select(ChatAttachment).where(ChatAttachment.id == item["id"])).scalar_one()
        assert stored.source_text == "" and stored.chunks_json == "[]" and stored.stored_path == ""


def test_l3_outbound_and_multiple_attachments_are_rejected(client, auth):
    headers = auth("attachment-l3-user")
    response = client.post(
        "/api/attachments", files={"file": ("private.md", b"secret", "text/markdown")},
        data={"confidentiality": "L3", "allow_outbound": "true"}, headers=headers,
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "l3_outbound_forbidden"
    unsupported = client.post(
        "/api/attachments", files={"file": ("payload.svg", b"<svg/>", "image/svg+xml")},
        data={"confidentiality": "L1", "allow_outbound": "false"}, headers=headers,
    )
    assert unsupported.status_code == 422
    multiple = client.post("/api/chat", json={
        "user_id": "attachment-l3-user", "message": "test", "attachment_ids": ["one", "two"],
    }, headers=headers)
    assert multiple.status_code == 422


def test_document_online_scope_and_provider_change(client, auth, monkeypatch):
    headers = auth("attachment-online-user")
    monkeypatch.setenv("TEST_ATTACHMENT_DEEPSEEK_KEY", "synthetic")
    monkeypatch.setattr(attachments, "_online_spec", lambda **kwargs: deepseek_spec())
    content = "\n\n".join(f"## Section {index}\n\nUnique content {index} with unrelated details." for index in range(6)).encode()
    item = upload(client, headers, content=content, confidentiality="L2", allow_outbound="true")
    assert item["processing_mode"] == "online_text"
    assert item["provider"] == "deepseek" and item["model"] == "deepseek-flash"
    call = Mock(return_value=SimpleNamespace(content="Section 3 is relevant [附件1]"))
    monkeypatch.setattr(attachments.providers, "call", call)
    response = client.post("/api/chat", json={
        "user_id": "attachment-online-user", "message": "Which section has Unique content 3?",
        "attachment_ids": [item["id"]],
    }, headers=headers)
    assert response.status_code == 200, response.text
    thread_id = response.json()["thread_id"]
    prompt = call.call_args.kwargs["user"]
    assert "Unique content 3" in prompt
    assert "Section 5" not in prompt

    monkeypatch.setattr(attachments, "_online_spec", lambda **kwargs: ModelSpec(
        id="other", model="other", vendor="other", base_url="https://example.test/v1", api_key_env=("TEST_ATTACHMENT_DEEPSEEK_KEY",),
    ))
    changed = client.post("/api/chat", json={
        "user_id": "attachment-online-user", "message": "again", "thread_id": thread_id, "attachment_ids": [item["id"]],
    }, headers=headers)
    assert changed.status_code == 409, changed.text
    assert changed.json()["detail"]["code"] == "attachment_provider_changed"


def test_image_local_ocr_never_uses_online_vision(client, auth, monkeypatch):
    headers = auth("attachment-local-image")
    monkeypatch.setattr(attachments, "ocr_image", lambda path: {"texts": ["手写内容"], "scores": [0.99], "boxes": []})
    online = Mock()
    monkeypatch.setattr(attachments.providers, "_post", online)
    monkeypatch.setattr(attachments, "generate", lambda prompt, **kwargs: {"text": "已整理手写内容", "model": "local", "network": "denied"})
    item = upload(client, headers, content=image_bytes(), filename="note.png")
    assert item["status"] == "ready" and item["processing_mode"] == "local_ocr"
    response = client.post("/api/chat", json={
        "user_id": "attachment-local-image", "message": "整理这段手写内容",
        "attachment_ids": [item["id"]],
    }, headers=headers)
    assert response.status_code == 200, response.text
    online.assert_not_called()


def test_deepseek_vision_uses_image_content_not_file_path(client, auth, monkeypatch):
    headers = auth("attachment-vision-user")
    monkeypatch.setenv("TEST_ATTACHMENT_DEEPSEEK_KEY", "synthetic")
    monkeypatch.setattr(attachments, "_online_spec", lambda **kwargs: deepseek_spec())
    monkeypatch.setattr(attachments, "ocr_image", Mock(side_effect=AssertionError("online image must not use local OCR")))
    item = upload(client, headers, content=image_bytes(), filename="chart.png", confidentiality="L2", allow_outbound="true")
    assert item["status"] == "ready" and item["processing_mode"] == "deepseek_vision"
    captured = {}
    def fake_post(_spec, _url, _headers, body, _timeout):
        captured.update(body)
        return {"choices": [{"message": {"content": "This is a chart [附件1]"}}]}
    monkeypatch.setattr(attachments.providers, "_post", fake_post)
    response = client.post("/api/chat", json={
        "user_id": "attachment-vision-user", "message": "What is in this image?",
        "attachment_ids": [item["id"]],
    }, headers=headers)
    assert response.status_code == 200, response.text
    user = captured["messages"][1]
    blocks = user["content"]
    assert blocks[0]["text"] == "What is in this image?"
    assert blocks[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert item["id"] not in json.dumps(captured)
    assert str(Path.cwd()) not in json.dumps(captured)


def test_expired_attachment_clears_content(client, auth):
    headers = auth("attachment-expired-user")
    item = upload(client, headers)
    from app.data_access import user_scope
    from app.data_access.attachments import ChatAttachment
    from app.db import memory_session
    from sqlalchemy import select
    with user_scope("attachment-expired-user"), memory_session() as conn:
        row = conn.execute(select(ChatAttachment).where(ChatAttachment.id == item["id"])).scalar_one()
        row.expires_at = "2000-01-01T00:00:00+00:00"
        conn.flush()
        path = Path(row.stored_path)
    detail = client.get(f"/api/attachments/{item['id']}", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["data"]["attachment"]["status"] == "expired"
    assert not path.exists()
