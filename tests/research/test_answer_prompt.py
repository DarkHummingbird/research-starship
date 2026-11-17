from __future__ import annotations

from types import SimpleNamespace

from app.gateway.config import ModelSpec
from app.research.gateway import configured_model_sender


def test_answer_prompt_uses_source_labels_not_internal_chunk_ids(monkeypatch):
    spec = ModelSpec(
        id="deepseek-chat", model="deepseek-chat", vendor="deepseek",
        base_url="https://api.deepseek.test/v1", api_key_env=("TEST_ANSWER_PROMPT_KEY",),
    )
    monkeypatch.setenv("TEST_ANSWER_PROMPT_KEY", "synthetic")
    monkeypatch.setattr("app.gateway.config.get_config", lambda: SimpleNamespace(models={spec.id: spec}))
    captured = {}

    def fake_call(_spec, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(content="依据 [S1] 的回答")

    monkeypatch.setattr("app.gateway.providers.call", fake_call)
    approval = {
        "model_id": spec.id, "provider": spec.vendor, "destination": spec.base_url,
        "scope": {"fragments": [
            {"document_id": None, "chunk_id": "query", "snippet": "问题", "chapter": "用户问题"},
            {"document_id": "doc1", "chunk_id": "doc1:v1:c3", "snippet": "资料内容", "chapter": "第 5 页"},
        ]},
    }
    result = configured_model_sender(approval)
    assert result == "依据 [S1] 的回答"
    assert "doc1:v1:c3" not in captured["user"]
    assert "[S1]" in captured["user"]
    assert "第 5 页" in captured["user"]
    assert "[用户问题]" in captured["user"]
    assert "不要使用内部片段 ID" in captured["system"]