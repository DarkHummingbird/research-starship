from __future__ import annotations

SPARSE = "# Sparse Study\n\n## Method\nBM25 uses term matching and inverse document frequency.\n\n## Baseline\nDense Retrieval is the comparison baseline.\n\n## Results\nBM25 reaches 87.4% accuracy on OPEN-DEMO-2."
DENSE = "# Dense Study\n\n## Method\nDense Retrieval uses semantic vector similarity.\n\n## Baseline\nBM25 is the comparison baseline.\n\n## Results\nDense Retrieval reaches 82.1% accuracy on OPEN-DEMO-2."


def _project(client, headers, name):
    response = client.post("/api/research/projects", json={"name": name}, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["data"]


def _paper(client, headers, project_id, filename, content, method):
    document = client.post(
        f"/api/research/projects/{project_id}/documents",
        files={"file": (filename, content.encode(), "text/markdown")},
        data={"confidentiality": "L1", "document_type": "paper"}, headers=headers,
    ).json()["data"]
    source = next(item for item in document["sources"] if item["chapter"] == "Method")
    metric_source = next(item for item in document["sources"] if "Results" in item["chapter"])
    payload = {
        "schema_version": "research-paper/v1", "record_type": "paper_statement",
        "method": [{
            "statement": f"{method} uses a retrieval method",
            "evidence": {"chunk_id": source["id"], "quote": source["content"]},
        }],
        "baseline": [], "datasets": [],
        "metrics": [{
            "statement": f"{method} reaches a synthetic accuracy", "value": "87.4", "unit": "%",
            "evidence": {"chunk_id": metric_source["id"], "quote": metric_source["content"]},
        }],
        "limitations": [], "suspicious": [],
    }
    updated = client.put(
        f"/api/research/documents/{document['id']}/analysis",
        json={"expected_version": 1, "analysis": payload}, headers=headers,
    )
    assert updated.status_code == 200, updated.text
    reviewed = client.post(
        f"/api/research/documents/{document['id']}/review",
        json={"expected_version": 1, "approved": True}, headers=headers,
    )
    assert reviewed.status_code == 200, reviewed.text
    return document


def _chat(client, headers, project_id, message, **extra):
    return client.post(
        f"/api/research/projects/{project_id}/chat",
        json={"message": message, "provider": "local", **extra}, headers=headers,
    )


def test_shared_method_across_papers_requires_paper_scope(client, auth):
    headers = auth("paper-scope-user")
    project = _project(client, headers, "论文范围")
    first = _paper(client, headers, project["id"], "sparse.md", SPARSE, "BM25")
    second = _paper(
        client, headers, project["id"], "sparse-variant.md",
        SPARSE.replace("Sparse Study", "Sparse Study Extended", 1), "BM25",
    )
    _paper(client, headers, project["id"], "dense.md", DENSE, "Dense Retrieval")

    response = _chat(client, headers, project["id"], "BM25 在 OPEN-DEMO-2 上的准确率是多少")
    assert response.status_code == 200, response.text
    result = response.json()["data"]
    assert result["status"] == "needs_clarification"
    assert result["clarification"]["kind"] == "paper_scope"
    candidates = result["clarification"]["entities"][0]["candidates"]
    assert {item["document_id"] for item in candidates} == {first["id"], second["id"]}
    assert result["sources"] == []

    selected = _chat(
        client, headers, project["id"], "BM25 在 OPEN-DEMO-2 上的准确率是多少",
        query_plan=result["query_plan"], selected_document_ids=[second["id"]],
    )
    assert selected.status_code == 200, selected.text
    answered = selected.json()["data"]
    assert answered["status"] == "answered"
    assert {item["document_id"] for item in answered["sources"]} == {second["id"]}
    assert "clarification" not in answered["query_plan"]


def test_citation_quote_is_exact_span_of_chunk(client, auth):
    headers = auth("citation-span-user")
    project = _project(client, headers, "引用跨度")
    document = _paper(client, headers, project["id"], "sparse.md", SPARSE, "BM25")
    chunks = {item["id"]: item["content"] for item in document["sources"]}

    response = _chat(client, headers, project["id"], "BM25 term matching accuracy")
    assert response.status_code == 200, response.text
    sources = response.json()["data"]["sources"]
    assert sources
    source = next(item for item in sources if item["chunk_id"] in chunks)
    assert source["quote"]
    assert chunks[source["chunk_id"]][source["quote_start"]:source["quote_end"]] == source["quote"]
    assert source["quote_start"] < source["quote_end"]


def test_single_question_across_papers_is_marked_cross_document(client, auth):
    headers = auth("cross-document-user")
    project = _project(client, headers, "跨文献提示")
    _paper(client, headers, project["id"], "sparse.md", SPARSE, "BM25")
    _paper(client, headers, project["id"], "dense.md", DENSE, "Dense Retrieval")

    response = _chat(client, headers, project["id"], "accuracy on OPEN-DEMO-2", allow_all_documents=True)
    assert response.status_code == 200, response.text
    result = response.json()["data"]
    assert result["status"] == "answered"
    scope = result["query_plan"].get("document_scope") or {}
    assert scope.get("cross_document") is True
    assert len(scope.get("document_ids") or []) > 1
    assert "多篇论文" in result["query_plan"].get("retrieval_warning", "")
    assert any(item.get("cross_document") for item in result["sources"])


def test_unrelated_question_is_flagged_as_weak_evidence(client, auth):
    headers = auth("weak-evidence-user")
    project = _project(client, headers, "弱证据提示")
    _paper(client, headers, project["id"], "sparse.md", SPARSE, "BM25")

    response = _chat(client, headers, project["id"], "quantum decoherence in superconducting qubits")
    assert response.status_code == 200, response.text
    result = response.json()["data"]
    if result["sources"]:
        assert "词面重合很少" in result["query_plan"].get("retrieval_warning", "")
    else:
        assert "未找到可核验证据" in result["answer"]

def test_multi_paper_question_without_method_name_asks_scope(client, auth):
    headers = auth("paper-scope-generic-user")
    project = _project(client, headers, "范围澄清")
    _paper(client, headers, project["id"], "sparse.md", SPARSE, "BM25")
    _paper(client, headers, project["id"], "dense.md", DENSE, "Dense Retrieval")

    response = _chat(client, headers, project["id"], "OPEN-DEMO-2 上的准确率是多少")
    assert response.status_code == 200, response.text
    result = response.json()["data"]
    assert result["status"] == "needs_clarification"
    assert result["clarification"]["kind"] == "paper_scope"
    candidates = result["clarification"]["entities"][0]["candidates"]
    assert len(candidates) == 2

    answered = _chat(
        client, headers, project["id"], "OPEN-DEMO-2 上的准确率是多少",
        query_plan=result["query_plan"], allow_all_documents=True,
    )
    assert answered.status_code == 200, answered.text
    payload = answered.json()["data"]
    assert payload["status"] == "answered"
    assert "clarification" not in payload["query_plan"]


def test_unique_method_name_limits_scope_without_clarification(client, auth):
    headers = auth("implicit-scope-user")
    project = _project(client, headers, "唯一方法限定")
    sparse = _paper(client, headers, project["id"], "sparse.md", SPARSE, "BM25")
    _paper(client, headers, project["id"], "dense.md", DENSE, "Dense Retrieval")

    response = _chat(client, headers, project["id"], "BM25 的准确率是多少")
    assert response.status_code == 200, response.text
    result = response.json()["data"]
    assert result["status"] == "answered"
    assert result["sources"]
    assert {item["document_id"] for item in result["sources"]} == {sparse["id"]}
    assert result["query_plan"].get("implicit_document_id") == sparse["id"]
    assert "限定" in result["query_plan"].get("scope_note", "")
