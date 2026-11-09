from __future__ import annotations

import io
from pathlib import Path

import pytest
from docx import Document
from docx.shared import Inches

from app.research import service
from app.research.local_components import component_status, embed_texts


P0_ROOT = Path(__file__).resolve().parents[3]
TUTORIAL = (P0_ROOT / "测试样本" / "虚构Agent使用教程.md").read_bytes()


@pytest.fixture
def user(auth, tmp_path, monkeypatch):
    from app.data_access import user_scope

    monkeypatch.setenv("RESEARCH_DATA_DIR", str(tmp_path / "research"))
    uid = "p2-document-user"
    with user_scope(uid):
        yield uid, auth(uid)


def project(client, user, name="P2 多格式"):
    _, headers = user
    response = client.post("/api/research/projects", json={"name": name}, headers=headers)
    assert response.status_code == 201
    return response.json()["data"]


def upload(client, user, project_id, filename, content, level="L1", doc_type="paper"):
    _, headers = user
    return client.post(
        f"/api/research/projects/{project_id}/documents",
        files={"file": (filename, content, "application/octet-stream")},
        data={"confidentiality": level, "document_type": doc_type}, headers=headers,
    )


def test_docx_tex_csv_html_parsers_and_p1_markdown_compatibility(client, user):
    project_id = project(client, user)["id"]
    word = Document()
    word.add_heading("合成论文", 0)
    word.add_heading("1. 方法", level=1)
    word.add_paragraph("使用受控检索增强生成，不执行真实实验。")
    table = word.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "数据集"
    table.cell(0, 1).text = "指标"
    table.cell(1, 0).text = "SYNTH"
    table.cell(1, 1).text = "87.4%"
    buffer = io.BytesIO()
    word.save(buffer)
    response = upload(client, user, project_id, "paper.docx", buffer.getvalue())
    assert response.status_code == 201, response.text
    doc = response.json()["data"]
    assert any(source["chapter"] == "表 1" for source in doc["sources"])
    assert doc["embedding_status"] in ("ready", "degraded")

    response = upload(client, user, project_id, "paper.tex", rb"\section{Method}\nUse evidence. \section{Limits}\nSmall sample.")
    assert response.status_code == 201, response.text
    assert [source["chapter"] for source in response.json()["data"]["sources"]] == ["Method", "Limits"]

    response = upload(client, user, project_id, "results.csv", "dataset,accuracy\nSYNTH,87.4%\n".encode())
    assert response.status_code == 201, response.text
    assert response.json()["data"]["sources"][0]["locator"]["row_start"] == 2

    response = upload(client, user, project_id, "page.html", b"<title>Evidence</title><script>bad()</script><p>Grounded answer.</p>")
    assert response.status_code == 201, response.text
    assert "Grounded answer." in response.json()["data"]["source_text"]
    assert "bad()" not in response.json()["data"]["source_text"]

    response = upload(client, user, project_id, "tutorial.md", TUTORIAL, doc_type="tutorial")
    assert response.status_code == 201, response.text
    document = response.json()["data"]
    _, headers = user
    approved = client.post(
        f"/api/research/documents/{document['id']}/review",
        json={"expected_version": 1, "approved": True}, headers=headers,
    )
    assert approved.status_code == 200, approved.text


def test_scanned_pdf_manual_offline_ocr_and_review_gate(client, user):
    if not component_status()["ocr"]["available"]:
        pytest.skip("local OCR assets unavailable")
    fitz = pytest.importorskip("fitz")
    sample = P0_ROOT / "测试样本" / "OCR合成样例.png"
    pdf = fitz.open()
    page = pdf.new_page(width=sample.stat().st_size * 0 + 900, height=500)
    page.insert_image(fitz.Rect(20, 20, 880, 480), filename=str(sample))
    content = pdf.tobytes()
    pdf.close()
    project_id = project(client, user)["id"]
    response = upload(client, user, project_id, "scan.pdf", content, "L3")
    assert response.status_code == 201, response.text
    document = response.json()["data"]
    assert document["processing_status"] == "needs_ocr"
    assert document["embedding_status"] == "paused"
    _, headers = user
    blocked = client.post(
        f"/api/research/documents/{document['id']}/review",
        json={"expected_version": 1, "approved": True}, headers=headers,
    )
    assert blocked.status_code == 409

    response = client.post(f"/api/research/documents/{document['id']}/ocr", headers=headers)
    assert response.status_code == 200, response.text
    result = response.json()["data"]
    assert result["ocr_pages"] == [1]
    assert result["version"] == 2
    assert result["processing_status"] == "ready"
    assert result["embedding_status"] == "ready"
    assert "Research agent" in result["source_text"]
    assert "87.4%" in result["source_text"]
    assert result["sources"][0]["locator"]["extraction"] == "offline_ocr"


def test_real_bge_embedding_is_offline_and_normalized():
    if not component_status()["embedding"]["available"]:
        pytest.skip("local embedding assets unavailable")
    result = embed_texts(["默认搜索当前课题", "保密资料只能本地处理"])
    assert result["dimensions"] == 1024
    assert len(result["vectors"]) == 2
    assert all(abs(sum(value * value for value in vector) - 1.0) < 1e-5 for vector in result["vectors"])
