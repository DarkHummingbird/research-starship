from __future__ import annotations

import math
from typing import Any

from app.gateway import providers
from app.gateway.config import ModelSpec, get_config


def embedding_spec() -> ModelSpec:
    return get_config().embedding_model("memory_embed")


def rerank_spec() -> ModelSpec:
    return get_config().rerank_model("memory_rerank")


def embed_texts(spec: ModelSpec, texts: list[str], *, timeout: float = 120) -> dict[str, Any]:
    api_key = spec.api_key()
    if not api_key:
        raise providers.ProviderError("no_key", "research embedding is not configured")
    data = providers._post(
        spec,
        spec.base_url.rstrip("/") + "/embeddings",
        {"Authorization": f"Bearer {api_key}"},
        {**spec.params, "model": spec.model, "input": texts, "encoding_format": "float"},
        min(timeout, spec.timeout or timeout),
    )
    try:
        rows = data["data"]
        if len(rows) != len(texts):
            raise ValueError("embedding count mismatch")
        ordered = sorted(rows, key=lambda row: row["index"])
        vectors = [row["embedding"] for row in ordered]
        if not vectors or any(not isinstance(vector, list) or not vector for vector in vectors):
            raise ValueError("invalid embeddings")
        dimensions = len(vectors[0])
        if any(len(vector) != dimensions for vector in vectors):
            raise ValueError("embedding dimensions mismatch")
        if any(type(value) not in (int, float) or not math.isfinite(value) for vector in vectors for value in vector):
            raise ValueError("embedding values must be finite")
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise providers.ProviderError("server", "invalid research embedding response") from exc
    return {"vectors": vectors, "model": spec.model, "dimensions": dimensions, "network": "online"}


def rerank(spec: ModelSpec, query: str, documents: list[str], *, timeout: float = 30) -> list[int]:
    api_key = spec.api_key()
    if not api_key:
        raise providers.ProviderError("no_key", "research rerank is not configured")
    data = providers._post(
        spec,
        spec.base_url.rstrip("/") + "/rerank",
        {"Authorization": f"Bearer {api_key}"},
        {**spec.params, "model": spec.model, "query": query, "documents": documents,
         "top_n": len(documents), "return_documents": False},
        min(timeout, spec.timeout or timeout),
    )
    try:
        rows = data["results"]
        if len(rows) != len(documents):
            raise ValueError("rerank result count mismatch")
        indices = [row["index"] for row in rows]
        if any(type(index) is not int for index in indices) or set(indices) != set(range(len(documents))):
            raise ValueError("invalid rerank indices")
        if any(type(row["relevance_score"]) not in (int, float) or not math.isfinite(row["relevance_score"]) for row in rows):
            raise ValueError("invalid rerank scores")
        return [row["index"] for row in sorted(rows, key=lambda row: -row["relevance_score"])]
    except (KeyError, TypeError, ValueError) as exc:
        raise providers.ProviderError("server", "invalid research rerank response") from exc


def check_services() -> dict[str, Any]:
    results: dict[str, Any] = {}
    generation = next((spec for spec in get_config().models.values() if spec.has_key()), None)
    if generation:
        try:
            response = providers.call(
                generation, api_key=generation.api_key(), system="连通性检查",
                user="只回复 OK", tier="fast", timeout=20, temperature=0, max_tokens=8,
            )
            results["generation"] = {"ok": bool(response.content), "model": generation.model}
        except Exception as exc:
            results["generation"] = {"ok": False, "error": getattr(exc, "kind", "error")}
    else:
        results["generation"] = {"ok": False, "error": "not_configured"}
    for name, spec in (("embedding", embedding_spec()), ("rerank", rerank_spec())):
        if not spec.has_key():
            results[name] = {"ok": False, "error": "not_configured"}
            continue
        try:
            if name == "embedding":
                embed_texts(spec, ["固定公开连通性测试"])
            else:
                rerank(spec, "固定公开连通性测试", ["测试文档一", "测试文档二"])
            results[name] = {"ok": True, "model": spec.model}
        except Exception as exc:
            results[name] = {"ok": False, "error": getattr(exc, "kind", "error"), "model": spec.model}
    return results
