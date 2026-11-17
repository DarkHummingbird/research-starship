from __future__ import annotations

from types import SimpleNamespace

from app.gateway.config import ModelSpec
from app.research import online


def test_capabilities_marks_chat_embedding_and_rerank_models(client, auth, monkeypatch):
    chat = ModelSpec(id="deepseek-chat", model="deepseek-chat", vendor="deepseek", base_url="https://api.deepseek.test/v1")
    embedding = ModelSpec(id="qwen-embed", model="qwen3.7-text-embedding", vendor="qwen")
    rerank = ModelSpec(id="qwen-rerank", model="qwen3-rerank", vendor="qwen")
    monkeypatch.setattr(
        "app.gateway.config.get_config",
        lambda: SimpleNamespace(models={item.id: item for item in (chat, embedding, rerank)}),
    )
    monkeypatch.setattr(online, "embedding_spec", lambda: embedding)
    monkeypatch.setattr(online, "rerank_spec", lambda: rerank)
    headers = auth("model-kind-user")

    response = client.get("/api/research/capabilities", headers=headers)
    assert response.status_code == 200, response.text
    models = response.json()["data"]["external_models"]
    kinds = {item["id"]: item["kind"] for item in models}
    assert kinds == {"deepseek-chat": "chat", "qwen-embed": "embedding", "qwen-rerank": "rerank"}