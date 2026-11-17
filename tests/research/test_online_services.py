from types import SimpleNamespace
from unittest.mock import Mock

from app.gateway.config import ModelSpec
from app.research import online


def synthetic_spec(model="synthetic/model"):
    return ModelSpec(
        id=model,
        model=model,
        vendor="synthetic",
        base_url="https://example.test/v1",
        api_key_env=("TEST_RESEARCH_ONLINE_KEY",),
    )


def test_terms_preserve_english_words_and_chinese_bigrams():
    from app.research.service import _terms

    terms = _terms("BERT accuracy improves retrieval 检索准确")
    assert {"bert", "accuracy", "improves", "retrieval"} <= terms
    assert "检索" in terms
    assert "准确" in terms


def test_bm25_uses_idf_term_frequency_and_length_normalization():
    from app.research.service import _bm25_context, _bm25_score, _term_counts

    documents = [_term_counts("shared"), _term_counts("shared other other other")]
    frequencies, average = _bm25_context(documents)
    short_score = _bm25_score({"shared"}, documents[0], frequencies, average, 2)
    long_score = _bm25_score({"shared"}, documents[1], frequencies, average, 2)
    assert short_score > long_score
    repeated = _term_counts("rare rare filler")
    single = _term_counts("rare filler")
    repeated_frequencies, repeated_average = _bm25_context([single, repeated])
    assert _bm25_score({"rare"}, repeated, repeated_frequencies, repeated_average, 2) > _bm25_score({"rare"}, single, repeated_frequencies, repeated_average, 2)
    rare_frequency, _ = _bm25_context([_term_counts("shared rare"), _term_counts("shared shared"), _term_counts("shared")])
    assert _bm25_score({"rare"}, _term_counts("rare"), rare_frequency, 1, 3) > 0


def test_online_embedding_validates_and_orders_vectors(monkeypatch):
    monkeypatch.setenv("TEST_RESEARCH_ONLINE_KEY", "synthetic")
    spec = synthetic_spec()
    monkeypatch.setattr(online.providers, "_post", lambda *args, **kwargs: {
        "data": [
            {"index": 1, "embedding": [0.0, 1.0]},
            {"index": 0, "embedding": [1.0, 0.0]},
        ],
    })
    result = online.embed_texts(spec, ["first", "second"])
    assert result["vectors"] == [[1.0, 0.0], [0.0, 1.0]]
    assert result["dimensions"] == 2


def test_online_rerank_returns_only_validated_indices(monkeypatch):
    monkeypatch.setenv("TEST_RESEARCH_ONLINE_KEY", "synthetic")
    spec = synthetic_spec("synthetic/rerank")
    monkeypatch.setattr(online.providers, "_post", lambda *args, **kwargs: {
        "results": [{"index": 0, "relevance_score": 0.1}, {"index": 1, "relevance_score": 0.9}],
    })
    assert online.rerank(spec, "query", ["first", "second"]) == [1, 0]


def test_l3_never_prepares_online_embedding():
    from app.research.ingest import _online_embedding

    row = SimpleNamespace(confidentiality="L3")
    assert _online_embedding("user", row, []) is None


def test_l2_online_index_requires_approval_then_runs_once(client, auth, tmp_path, monkeypatch):
    monkeypatch.setenv("RESEARCH_DATA_DIR", str(tmp_path / "research"))
    monkeypatch.setenv("TEST_RESEARCH_ONLINE_KEY", "synthetic")
    spec = synthetic_spec("synthetic/embed")
    monkeypatch.setattr(online, "embedding_spec", lambda: spec)
    calls = []

    def fake_embed(_spec, texts, **_kwargs):
        calls.append(list(texts))
        return {"vectors": [[1.0] + [0.0] * 1023 for _ in texts], "model": spec.model, "dimensions": 1024}

    monkeypatch.setattr(online, "embed_texts", fake_embed)
    headers = auth("online-index-user")
    project = client.post("/api/research/projects", json={"name": "在线索引"}, headers=headers).json()["data"]
    imported = client.post(
        f"/api/research/projects/{project['id']}/documents",
        files={"file": ("tutorial.md", "# 教程\n\n只有确认后才进入课题检索。", "text/markdown")},
        data={"confidentiality": "L2", "document_type": "tutorial"},
        headers=headers,
    )
    assert imported.status_code == 201, imported.text
    document = imported.json()["data"]
    assert document["embedding_status"] == "awaiting_approval"
    assert calls == []
    approval = document["approval"]

    approved = client.post(
        f"/api/research/outbound/{approval['id']}/decision", json={"approved": True}, headers=headers,
    )
    assert approved.status_code == 200
    indexed = client.post(
        f"/api/research/documents/{document['id']}/index",
        json={"expected_version": document["version"], "task_id": approval["task_id"], "approval_id": approval["id"]},
        headers=headers,
    )
    assert indexed.status_code == 200, indexed.text
    assert indexed.json()["data"]["embedding_status"] == "ready"
    assert len(calls) == 1


def test_l2_rerank_approval_binds_query_and_candidates(client, auth, tmp_path, monkeypatch):
    monkeypatch.setenv("RESEARCH_DATA_DIR", str(tmp_path / "research"))
    monkeypatch.setenv("TEST_RESEARCH_ONLINE_KEY", "synthetic")
    spec = synthetic_spec("synthetic/rerank")
    monkeypatch.setattr(online, "rerank_spec", lambda: spec)
    from app.research import ingest

    monkeypatch.setattr(ingest, "embed_texts", lambda texts: {
        "vectors": [[1.0] + [0.0] * 1023 for _ in texts], "model": "BAAI/bge-m3:onnx", "dimensions": 1024,
    })
    calls = []

    def fake_rerank(_spec, query, documents, **_kwargs):
        calls.append((query, list(documents)))
        return list(range(len(documents)))

    monkeypatch.setattr(online, "rerank", fake_rerank)
    headers = auth("online-rerank-user")
    project = client.post("/api/research/projects", json={"name": "在线重排"}, headers=headers).json()["data"]
    note = client.post(
        f"/api/research/projects/{project['id']}/study-notes",
        json={"title": "检索规则", "content": "只有用户确认后的资料才能进入正式检索。", "document_type": "research_note", "confidentiality": "L2"},
        headers=headers,
    ).json()["data"]
    reviewed = client.post(
        f"/api/research/documents/{note['id']}/review",
        json={"expected_version": note["version"], "approved": True}, headers=headers,
    )
    assert reviewed.status_code == 200
    for index in range(4):
        extra = client.post(
            f"/api/research/projects/{project['id']}/study-notes",
            json={"title": f"备用检索规则 {index}", "content": f"第 {index} 条关于正式检索的备用说明。", "document_type": "research_note", "confidentiality": "L2"},
            headers=headers,
        ).json()["data"]
        client.post(
            f"/api/research/documents/{extra['id']}/review",
            json={"expected_version": extra["version"], "approved": True}, headers=headers,
        )
    body = {"message": "什么资料能进入正式检索？", "provider": "local", "query_confidentiality": "L2"}
    pending = client.post(f"/api/research/projects/{project['id']}/chat", json=body, headers=headers)
    assert pending.status_code == 200, pending.text
    result = pending.json()["data"]
    assert result["status"] == "awaiting_outbound_approval"
    assert result["approval"]["purpose"] == "科研检索候选重排"
    assert calls == []
    approval = result["approval"]
    client.post(f"/api/research/outbound/{approval['id']}/decision", json={"approved": True}, headers=headers)
    completed = client.post(
        f"/api/research/projects/{project['id']}/chat",
        json={**body, "task_id": approval["task_id"], "approval_id": approval["id"]},
        headers=headers,
    )
    assert completed.status_code == 200, completed.text
    assert completed.json()["data"]["status"] == "answered"
    assert len(calls) == 1
    assert calls[0][0] == body["message"]
    assert 3 < len(calls[0][1]) <= 100


def test_online_configuration_check_does_not_call_unconfigured_services(monkeypatch):
    monkeypatch.delenv("TEST_RESEARCH_ONLINE_KEY", raising=False)
    monkeypatch.setattr(online, "embedding_spec", lambda: synthetic_spec("synthetic/embed"))
    monkeypatch.setattr(online, "rerank_spec", lambda: synthetic_spec("synthetic/rerank"))
    result = online.check_services()
    assert result["embedding"]["error"] == "not_configured"
    assert result["rerank"]["error"] == "not_configured"


def test_qwen_embedding_batches_at_twenty_and_preserves_order(monkeypatch):
    monkeypatch.setenv("TEST_RESEARCH_ONLINE_KEY", "synthetic")
    spec = ModelSpec(
        id="qwen-embed", model="qwen3.7-text-embedding", vendor="qwen",
        base_url="https://example.test/compatible-mode/v1", api_key_env=("TEST_RESEARCH_ONLINE_KEY",),
        params={"dimensions": 1024},
    )
    calls = []

    def fake_post(_spec, url, _headers, body, _timeout):
        calls.append((url, body))
        return {"data": [{"index": index, "embedding": [float(index), 1.0]}
                         for index in range(len(body["input"]))]}

    monkeypatch.setattr(online.providers, "_post", fake_post)
    result = online.embed_texts(spec, [f"text-{index}" for index in range(45)])
    assert [len(call[1]["input"]) for call in calls] == [20, 20, 5]
    assert all(call[0].endswith("/embeddings") for call in calls)
    assert all(call[1]["dimensions"] == 1024 for call in calls)
    assert len(result["vectors"]) == 45


def test_qwen_rerank_uses_plural_compatible_endpoint(monkeypatch):
    monkeypatch.setenv("TEST_RESEARCH_ONLINE_KEY", "synthetic")
    spec = ModelSpec(
        id="qwen-rerank", model="qwen3.7-text-rerank", vendor="qwen",
        base_url="https://example.test/compatible-api/v1", api_key_env=("TEST_RESEARCH_ONLINE_KEY",),
    )
    seen = []

    def fake_post(_spec, url, _headers, _body, _timeout):
        seen.append(url)
        return {"results": [{"index": 0, "relevance_score": 0.1}, {"index": 1, "relevance_score": 0.9}]}

    monkeypatch.setattr(online.providers, "_post", fake_post)
    assert online.rerank(spec, "query", ["first", "second"]) == [1, 0]
    assert seen == ["https://example.test/compatible-api/v1/reranks"]


def test_qwen_rag_file_routes_research_tasks_without_changing_chat(tmp_path, monkeypatch):
    import json
    from app.gateway.config import build_config

    path = tmp_path / "qwen-rag.json"
    path.write_text(json.dumps({
        "providers": {
            "qwen-embed": {"base_url": "https://example.test/compatible-mode/v1", "api_key_env": "DASHSCOPE_API_KEY", "vendor": "qwen"},
            "qwen-rerank": {"base_url": "https://example.test/compatible-api/v1", "api_key_env": "DASHSCOPE_API_KEY", "vendor": "qwen", "connect_timeout": 20},
        },
        "models": {
            "embed": {"provider": "qwen-embed", "model": "qwen3.7-text-embedding", "params": {"dimensions": 1024}},
            "rerank": {"provider": "qwen-rerank", "model": "qwen3.7-text-rerank"},
        },
        "embedding_tasks": {"memory_embed": "embed"},
        "rerank_tasks": {"memory_rerank": "rerank"},
    }), encoding="utf-8")
    monkeypatch.setenv("LLM_CONFIG_FILE", str(path))
    monkeypatch.setenv("DASHSCOPE_API_KEY", "synthetic")
    config = build_config()
    assert config.embedding_model().model == "qwen3.7-text-embedding"
    assert config.embedding_model().params["dimensions"] == 1024
    assert config.rerank_model().model == "qwen3.7-text-rerank"
    assert config.rerank_model().connect_timeout == 20


def test_provider_uses_model_specific_connect_timeout(monkeypatch):
    from app.gateway import providers

    spec = ModelSpec(
        id="qwen-rerank", model="qwen3.7-text-rerank", vendor="qwen",
        base_url="https://example.test/compatible-api/v1", api_key_env=("TEST_RESEARCH_ONLINE_KEY",),
        timeout=120, connect_timeout=20,
    )
    monkeypatch.setenv("TEST_RESEARCH_ONLINE_KEY", "synthetic")
    client = Mock()
    client.post.return_value = SimpleNamespace(status_code=200, json=lambda: {"results": []})
    monkeypatch.setattr(providers, "_client", lambda *_args: client)
    providers._post(spec, spec.base_url + "/reranks", {}, {}, 120)
    timeout = client.post.call_args.kwargs["timeout"]
    assert timeout.connect == 20
    assert timeout.read == 120


def test_query_embedding_network_failure_uses_bm25_without_retry(client, auth, tmp_path, monkeypatch):
    from app.gateway import providers

    monkeypatch.setenv("RESEARCH_DATA_DIR", str(tmp_path / "research"))
    monkeypatch.setenv("TEST_RESEARCH_ONLINE_KEY", "synthetic")
    spec = synthetic_spec("qwen-embed")
    monkeypatch.setattr(online, "embedding_spec", lambda: spec)
    monkeypatch.setattr(online, "embed_texts", lambda _spec, texts: {
        "vectors": [[1.0, 0.0] for _ in texts], "model": spec.model, "dimensions": 2,
    })
    headers = auth("query-embedding-network-fallback")
    project = client.post("/api/research/projects", json={"name": "查询向量降级"}, headers=headers).json()["data"]
    imported = client.post(
        f"/api/research/projects/{project['id']}/documents",
        files={"file": ("tutorial.md", "# 教程\n\n只有用户确认后的资料才能进入正式检索。", "text/markdown")},
        data={"confidentiality": "L1", "document_type": "tutorial"}, headers=headers,
    )
    document = imported.json()["data"]
    client.post(f"/api/research/documents/{document['id']}/review",
                json={"expected_version": 1, "approved": True}, headers=headers)
    calls = Mock(side_effect=providers.ProviderError("timeout", "synthetic query timeout"))
    monkeypatch.setattr(online, "embed_texts", calls)
    response = client.post(f"/api/research/projects/{project['id']}/chat", json={
        "message": "什么资料能进入正式检索？", "provider": "local", "query_confidentiality": "L1",
    }, headers=headers)
    assert response.status_code == 200, response.text
    result = response.json()["data"]
    assert result["status"] == "answered"
    assert result["query_plan"]["embedding_status"] == "degraded_local_lexical"
    assert "BM25" in result["retrieval_warning"]
    assert calls.call_count == 1
