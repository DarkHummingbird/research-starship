from __future__ import annotations

import time
from unittest.mock import Mock

import fitz

from app.gateway.config import ModelSpec
from app.research import local_mode, online_ocr


def scanned_pdf() -> bytes:
    source = fitz.open()
    page = source.new_page(width=300, height=120)
    page.insert_text((20, 60), "Research agent 87.4%", fontsize=16)
    image = page.get_pixmap().tobytes("png")
    source.close()
    pdf = fitz.open()
    pdf.new_page(width=300, height=120).insert_image(fitz.Rect(0, 0, 300, 120), stream=image)
    content = pdf.tobytes()
    pdf.close()
    return content


def project(client, headers, name="DeepSeek OCR"):
    return client.post("/api/research/projects", json={"name": name}, headers=headers).json()["data"]


def upload(client, headers, project_id, level="L1"):
    response = client.post(
        f"/api/research/projects/{project_id}/documents",
        files={"file": ("scan.pdf", scanned_pdf(), "application/pdf")},
        data={"confidentiality": level, "document_type": "paper"}, headers=headers,
    )
    assert response.status_code == 201, response.text
    result = response.json()["data"]
    assert result["processing_status"] == "needs_ocr"
    return result


def configure_ocr(monkeypatch, response=None):
    spec = ModelSpec(
        id="deepseek-ocr", model="deepseek-ocr", vendor="deepseek",
        base_url="https://api.deepseek.test/v1", api_key_env=("TEST_DEEPSEEK_OCR_KEY",), timeout=180,
    )
    monkeypatch.setenv("TEST_DEEPSEEK_OCR_KEY", "synthetic")
    monkeypatch.setattr(online_ocr, "ocr_spec", lambda: spec)
    calls = Mock(return_value=response or {
        "id": "ocr-synthetic", "page_count": 1,
        "pages": [{
            "index": 0,
            "text": "<｜ref｜>text<｜/ref｜><｜box｜>[[100, 100, 900, 400]]<｜/box｜>Research agent 87.4%",
            "finish_reason": "stop",
        }],
        "usage": {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110},
    })
    monkeypatch.setattr(online_ocr, "recognize", calls)
    return calls


def disable_local(user_id):
    from app.data_access import user_scope

    with user_scope(user_id):
        local_mode.set_enabled(user_id, False)


def test_deepseek_ocr_returns_page_text_coordinates_and_highlight(client, auth, tmp_path, monkeypatch):
    monkeypatch.setenv("RESEARCH_DATA_DIR", str(tmp_path / "research"))
    uid = "deepseek-ocr-l1"
    headers = auth(uid)
    disable_local(uid)
    calls = configure_ocr(monkeypatch)
    project_id = project(client, headers)["id"]
    document = upload(client, headers, project_id)
    response = client.post(
        f"/api/research/documents/{document['id']}/ocr",
        json={"provider": "external", "expected_version": 1}, headers=headers,
    )
    assert response.status_code == 200, response.text
    result = response.json()["data"]
    assert calls.call_count == 1
    assert result["version"] == 2 and result["ocr_pages"] == [1]
    assert result["ocr_provider"] == "external:deepseek-ocr"
    assert result["ocr_usage"]["total_tokens"] == 110
    assert "Research agent 87.4%" in result["source_text"]
    assert "<｜ref｜>" not in result["source_text"]
    source = next(item for item in result["sources"] if item["locator"].get("extraction") == "deepseek_ocr")
    line = source["locator"]["ocr_lines"][0]
    assert line["box"] == [30.0, 12.0, 270.0, 48.0]
    highlighted = client.post(
        f"/api/research/documents/{document['id']}/highlight",
        json={"expected_version": 2, "chunk_id": source["id"], "quote": line["text"]}, headers=headers,
    )
    assert highlighted.status_code == 200, highlighted.text
    assert highlighted.content.startswith(b"\x89PNG\r\n\x1a\n")


def test_l2_deepseek_ocr_requires_approval_and_runs_once(client, auth, tmp_path, monkeypatch):
    monkeypatch.setenv("RESEARCH_DATA_DIR", str(tmp_path / "research"))
    uid = "deepseek-ocr-l2"
    headers = auth(uid)
    disable_local(uid)
    calls = configure_ocr(monkeypatch)
    project_id = project(client, headers)["id"]
    document = upload(client, headers, project_id, "L2")
    body = {"provider": "external", "expected_version": 1}
    pending = client.post(f"/api/research/documents/{document['id']}/ocr", json=body, headers=headers)
    assert pending.status_code == 200, pending.text
    result = pending.json()["data"]
    assert result["status"] == "awaiting_outbound_approval"
    assert result["approval"]["purpose"] == "DeepSeek 在线 OCR"
    assert result["approval"]["scope"]["fragments"][0]["chapter"] == "原始 PDF 整份文件"
    calls.assert_not_called()
    approval = result["approval"]
    client.post(f"/api/research/outbound/{approval['id']}/decision", json={"approved": True}, headers=headers)
    completed = client.post(
        f"/api/research/documents/{document['id']}/ocr",
        json={**body, "task_id": approval["task_id"], "approval_id": approval["id"]}, headers=headers,
    )
    assert completed.status_code == 200, completed.text
    assert completed.json()["data"]["version"] == 2
    assert calls.call_count == 1


def test_l3_deepseek_ocr_is_rejected_without_send(client, auth, tmp_path, monkeypatch):
    monkeypatch.setenv("RESEARCH_DATA_DIR", str(tmp_path / "research"))
    uid = "deepseek-ocr-l3"
    headers = auth(uid)
    calls = configure_ocr(monkeypatch)
    project_id = project(client, headers)["id"]
    document = upload(client, headers, project_id, "L3")
    response = client.post(
        f"/api/research/documents/{document['id']}/ocr",
        json={"provider": "external", "expected_version": 1}, headers=headers,
    )
    assert response.status_code == 403, response.text
    assert response.json()["detail"]["code"] == "l3_external_forbidden"
    calls.assert_not_called()


def test_deepseek_ocr_multipart_contract_and_page_validation(tmp_path, monkeypatch):
    path = tmp_path / "sample.pdf"
    path.write_bytes(b"synthetic-pdf")
    spec = ModelSpec(
        id="deepseek-ocr", model="deepseek-ocr", vendor="deepseek",
        base_url="https://api.deepseek.test/v1", api_key_env=("TEST_DEEPSEEK_OCR_KEY",),
    )
    monkeypatch.setenv("TEST_DEEPSEEK_OCR_KEY", "synthetic")
    monkeypatch.setattr(online_ocr, "ocr_spec", lambda: spec)
    seen = {}

    def fake_post(_spec, url, _headers, data, files, _timeout):
        seen.update({"url": url, "data": data, "filename": files["file"][0],
                     "content": files["file"][1].read()})
        return {"page_count": 1, "pages": [{"index": 0, "text": "文字", "finish_reason": "stop"}]}

    monkeypatch.setattr(online_ocr.providers, "_post_multipart", fake_post)
    result = online_ocr.recognize(path)
    assert result["pages"][0]["text"] == "文字"
    assert seen == {
        "url": "https://api.deepseek.test/v1/tools/ocr",
        "data": {"purpose": "text_recognition"}, "filename": "sample.pdf", "content": b"synthetic-pdf",
    }


def test_online_ocr_batch_operation(client, auth, tmp_path, monkeypatch):
    monkeypatch.setenv("RESEARCH_DATA_DIR", str(tmp_path / "research"))
    uid = "deepseek-ocr-batch"
    headers = auth(uid)
    disable_local(uid)
    calls = configure_ocr(monkeypatch)
    project_id = project(client, headers)["id"]
    document = upload(client, headers, project_id)
    created = client.post(
        f"/api/research/projects/{project_id}/batches",
        json={"document_ids": [document["id"]], "operation": "ocr_online"}, headers=headers,
    )
    assert created.status_code == 202, created.text
    batch_id = created.json()["data"]["id"]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        batch = client.get(f"/api/research/batches/{batch_id}", headers=headers).json()["data"]
        if batch["items"][0]["status"] == "pending_review":
            break
        time.sleep(0.02)
    assert batch["items"][0]["status"] == "pending_review", batch
    assert batch["items"][0]["operation"] == "ocr_online"
    assert calls.call_count == 1
