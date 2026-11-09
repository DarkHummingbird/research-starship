from __future__ import annotations

import json

import pytest

from app.research.generations import _validate_generated_content
from app.research.paper_analysis import _text_key
from app.research.service import ResearchError


def test_pdf_hyphenated_line_break_matches_original_word():
    assert _text_key("English-\nto-German") == _text_key("English-to-German")
    assert _text_key("fine-\ntuned") == _text_key("fine-tuned")
    assert _text_key("不同 语义") != _text_key("不同语义错误")


def test_latex_rejects_full_document_unverified_citations_and_missing_sources():
    valid = "\\section{Method}\n来源支持的局部段落 [S1]。"
    _validate_generated_content("latex", valid)
    invalid = [
        "\\documentclass{article}\n局部段落 [S1]",
        "\\begin{document}\n局部段落 [S1]\n\\end{document}",
        "\\section{Work}\n引用 \\cite{unknown} [S1]",
        "\\section{Work}\n\\begin{thebibliography}{1}\\bibitem{x} 待补\\end{thebibliography} [S1]",
        "\\section{Work}\n没有来源标记",
    ]
    for content in invalid:
        with pytest.raises(ResearchError) as blocked:
            _validate_generated_content("latex", content)
        assert blocked.value.code == "generation_validation_failed"


def test_numeric_table_rows_require_source_markers():
    _validate_generated_content("table", "| 模型 | 结果 |\n| --- | --- |\n| FF Only | 74.9% [S1] |")
    with pytest.raises(ResearchError) as blocked:
        _validate_generated_content("table", "| 模型 | 结果 |\n| --- | --- |\n| FF Only | 74.9% |")
    assert blocked.value.code == "generation_validation_failed"


def test_table_rejects_swapped_model_metric_attribution():
    matches = [{
        "content": (
            "Base (P=16)\nViT 86M 77.9\nFF Only 62M 74.9\n"
            "Large (P=32)\nViT 306M 71.2\nFF Only 206M 71.4\n"
            "Tiny\nAttention Only 4.0M 28.2"
        ),
    }]
    _validate_generated_content("table", "| FF Only Large | 71.4% | [S1] |", matches)
    _validate_generated_content("table", "| ViT Large | 71.2% | [S1] |", matches)
    _validate_generated_content("table", "| FF Only Base (Feed-Forward Only) | 74.9% | [S1] |", matches)
    with pytest.raises(ResearchError) as blocked:
        _validate_generated_content("table", "| FF Only Large | 71.2% | [S1] |", matches)
    assert blocked.value.code == "generation_validation_failed"
    with pytest.raises(ResearchError):
        _validate_generated_content("table", "| Top-1 Accuracy | 71.4% | [S1] |", matches)
    narrative = [{
        "content": (
            "a ViT/DeiT-base-sized model obtains 74.9% top-1 accuracy, "
            "compared to 77.9% and 79.9% for ViT and DeiT respectively."
        ),
    }]
    _validate_generated_content("table", "| ViT Base (Attention) | 77.9% | [S1] |", narrative)
    corrected = _validate_generated_content(
        "table", "| FF Only Base (Feed-Forward Only) | 74.9% | [S1] |",
        [{"content": "unrelated abstract"}, matches[0]],
    )
    assert "[S2]" in corrected and "唯一证据校正" in corrected


def test_latex_without_style_samples_adds_deterministic_disclosure(client, auth, tmp_path, monkeypatch):
    from app.research import generations

    monkeypatch.setenv("RESEARCH_DATA_DIR", str(tmp_path / "research"))
    headers = auth("p4-style-disclosure-user")
    project = client.post("/api/research/projects", json={"name": "无样本提示"}, headers=headers)
    project_id = project.json()["data"]["id"]
    baseline = client.post(
        f"/api/research/projects/{project_id}/study-notes",
        json={"title": "合成基线", "content": "合成资料：模型在 SYNTH-10 上为 80.0%。", "document_type": "baseline", "confidentiality": "L3"},
        headers=headers,
    ).json()["data"]
    reviewed = client.post(
        f"/api/research/documents/{baseline['id']}/review",
        json={"expected_version": 1, "approved": True}, headers=headers,
    )
    assert reviewed.status_code == 200, reviewed.text
    monkeypatch.setattr(generations, "generate", lambda *_args, **_kwargs: {
        "text": "\\section{Experiments}\n合成基线为 80.0\\% [S1]。 [end of text]", "model": "test-double",
    })
    response = client.post(
        f"/api/research/projects/{project_id}/generations",
        json={"kind": "latex", "query": "模仿我的风格写局部实验设置", "document_ids": [baseline["id"]]},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    result = response.json()["data"]
    assert "未提供个人写作样本" in result["content"]
    assert "未模仿用户风格" in result["content"]
    assert "请复核" in result["content"]
    assert "[end of text]" not in result["content"]
    assert result["history"]["style_sample_count"] == 0


def test_mixed_model_facts_are_quarantined_with_raw_output_and_review_gate(client, auth, tmp_path, monkeypatch):
    from app.research import paper_analysis

    monkeypatch.setenv("RESEARCH_DATA_DIR", str(tmp_path / "research"))
    headers = auth("p4-quarantine-user")
    project = client.post("/api/research/projects", json={"name": "字段级隔离"}, headers=headers)
    project_id = project.json()["data"]["id"]
    uploaded = client.post(
        f"/api/research/projects/{project_id}/documents",
        files={"file": ("paper.md", b"# Synthetic paper\n\nSafeRAG achieves 87.4% accuracy on SYNTH-10.", "text/markdown")},
        data={"document_type": "paper", "confidentiality": "L3"}, headers=headers,
    )
    assert uploaded.status_code == 201, uploaded.text
    document = uploaded.json()["data"]
    chunk = document["sources"][0]
    valid_fact = {
        "statement": "SafeRAG achieves 87.4% accuracy on SYNTH-10", "value": "87.4", "unit": "%",
        "evidence": {"chunk_id": chunk["id"], "quote": "SafeRAG achieves 87.4% accuracy on SYNTH-10.", "match_type": "exact"},
    }
    invalid_fact = {
        "statement": "Unverified result", "evidence": {"chunk_id": chunk["id"], "quote": "not in source"},
    }
    raw = {
        "schema_version": "research-paper/v1", "record_type": "paper_statement",
        "method": [], "baseline": [], "datasets": [], "metrics": [valid_fact, invalid_fact],
        "limitations": [], "suspicious": [],
    }
    monkeypatch.setattr(paper_analysis, "generate", lambda *_args, **_kwargs: {
        "text": json.dumps(raw) + " [end of text]", "model": "test-double",
    })
    response = client.post(f"/api/research/documents/{document['id']}/analysis", headers=headers)
    assert response.status_code == 200, response.text
    result = response.json()["data"]
    assert result["analysis_status"] == "needs_review"
    assert len(result["analysis"]["metrics"]) == 1
    assert "Unverified result" in result["raw_model_output"]
    assert "已隔离" in result["processing_error"]
    blocked = client.post(
        f"/api/research/documents/{document['id']}/review",
        json={"expected_version": 1, "approved": True}, headers=headers,
    )
    assert blocked.status_code == 409
    assert client.get(
        f"/api/research/projects/{project_id}/memories", headers=headers,
    ).json()["data"]["items"] == []
    incomplete = client.put(
        f"/api/research/documents/{document['id']}/analysis",
        json={"expected_version": 1, "analysis": result["analysis"]}, headers=headers,
    )
    assert incomplete.status_code == 409
    issue = next(item for item in result["analysis"]["suspicious"] if item.startswith("已隔离："))
    corrected = client.put(
        f"/api/research/documents/{document['id']}/analysis",
        json={"expected_version": 1, "analysis": result["analysis"],
              "resolutions": [{"issue": issue, "action": "discarded"}]}, headers=headers,
    )
    assert corrected.status_code == 200, corrected.text
    assert corrected.json()["data"]["analysis_status"] == "ready"
    approved = client.post(
        f"/api/research/documents/{document['id']}/review",
        json={"expected_version": 1, "approved": True}, headers=headers,
    )
    assert approved.status_code == 200, approved.text
    detail = client.get(f"/api/research/documents/{document['id']}", headers=headers).json()["data"]
    assert "Unverified result" in detail["raw_model_output"]


def test_rejected_generation_can_be_corrected_but_not_exported_before_review(client, auth, tmp_path, monkeypatch):
    from app.research import generations

    monkeypatch.setenv("RESEARCH_DATA_DIR", str(tmp_path / "research"))
    headers = auth("p4-generation-correction-user")
    project = client.post("/api/research/projects", json={"name": "拒绝稿修正"}, headers=headers)
    project_id = project.json()["data"]["id"]
    baseline = client.post(
        f"/api/research/projects/{project_id}/study-notes",
        json={"title": "FF 基线", "content": "FF Only achieves 74.9% on SYNTH-10.", "document_type": "baseline", "confidentiality": "L3"},
        headers=headers,
    ).json()["data"]
    reviewed = client.post(
        f"/api/research/documents/{baseline['id']}/review",
        json={"expected_version": 1, "approved": True}, headers=headers,
    )
    assert reviewed.status_code == 200, reviewed.text
    monkeypatch.setattr(generations, "generate", lambda *_args, **_kwargs: {
        "text": "| FF Only | 74.9% |", "model": "test-double",
    })
    rejected = client.post(
        f"/api/research/projects/{project_id}/generations",
        json={"kind": "table", "query": "生成对比表", "document_ids": [baseline["id"]]}, headers=headers,
    )
    assert rejected.status_code == 422, rejected.text
    detail = rejected.json()["detail"]
    generation_id = detail["generation_id"]
    current = client.get(f"/api/research/generations/{generation_id}", headers=headers).json()["data"]
    assert current["status"] == "rejected"
    assert current["content"].startswith("| FF Only | 74.9% |")
    blocked = client.post(
        f"/api/research/generations/{generation_id}/export", json={"filename": "table.md"}, headers=headers,
    )
    assert blocked.status_code == 409
    archived = client.post(f"/api/research/generations/{generation_id}/archive", headers=headers)
    assert archived.status_code == 409
    corrected = client.put(
        f"/api/research/generations/{generation_id}",
        json={"content": "| FF Only | 74.9% | [S1] |"}, headers=headers,
    )
    assert corrected.status_code == 200, corrected.text
    assert corrected.json()["data"]["status"] == "pending"
    exported = client.post(
        f"/api/research/generations/{generation_id}/export", json={"filename": "table.md"}, headers=headers,
    )
    assert exported.status_code == 201, exported.text
    assert exported.json()["data"]["remote_exported"] is False
    download = client.get(exported.json()["data"]["download_url"], headers=headers)
    assert download.status_code == 200
    assert "74.9%" in download.content.decode("utf-8")
    missing = client.get(
        f"/api/research/generations/{generation_id}/download?filename=missing.md", headers=headers,
    )
    assert missing.status_code == 404


def test_public_retry_honors_retry_after(monkeypatch):
    from app.research import external_sources

    class Response:
        content = b"ok"
        status_code = 200
        headers = {}

    unavailable = Response()
    unavailable.status_code = 503
    unavailable.headers = {"Retry-After": "1"}
    responses = iter([unavailable, Response()])
    calls = []
    sleeps = []
    monkeypatch.setattr(external_sources.httpx, "get", lambda *_args, **_kwargs: calls.append(1) or next(responses))
    monkeypatch.setattr(external_sources.time, "sleep", sleeps.append)
    response = external_sources._request(
        "https://export.arxiv.org/api/query", allowed_hosts={"export.arxiv.org"}, retries=2,
    )
    assert response.status_code == 200
    assert len(calls) == 2
    assert sleeps == [1.0]


def test_local_generation_processes_are_serialized(monkeypatch):
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor
    from types import SimpleNamespace
    from app.research import local_components

    active = 0
    maximum = 0
    lock = threading.Lock()

    def fake_run(*_args, **_kwargs):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.05)
        with lock:
            active -= 1
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(local_components.subprocess, "run", fake_run)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda index: local_components.generate(f"test {index}"), range(2)))
    assert maximum == 1
    assert all(item["text"] == "ok" for item in results)
    assert local_components.component_status()["generative_llm"]["max_concurrent_generations"] == 1


def test_formula_notation_requires_manual_review():
    from app.research.ocr import has_formula_notation

    assert has_formula_notation("L = -Σ_i y_i log(ŷ_i)")
    assert has_formula_notation("Attention(Q,K,V) = softmax(QK^T/√d_k)V")
    assert not has_formula_notation("SafeRAG achieves 87.4% accuracy")
