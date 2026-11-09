from __future__ import annotations


def test_source_file_download_uses_stored_copy_and_owner_scope(client, auth, tmp_path, monkeypatch):
    monkeypatch.setenv("RESEARCH_DATA_DIR", str(tmp_path / "research"))
    uid = "p4-source-file-user"
    headers = auth(uid)
    project = client.post("/api/research/projects", json={"name": "原件下载"}, headers=headers)
    project_id = project.json()["data"]["id"]
    content = "# 合成资料\n\n来源正文。".encode("utf-8")
    uploaded = client.post(
        f"/api/research/projects/{project_id}/documents",
        files={"file": ("source.md", content, "text/markdown")},
        data={"confidentiality": "L3", "document_type": "tutorial"}, headers=headers,
    )
    assert uploaded.status_code == 201, uploaded.text
    document_id = uploaded.json()["data"]["id"]
    response = client.get(f"/api/research/documents/{document_id}/source-file", headers=headers)
    assert response.status_code == 200
    assert response.content == content
    assert "source.md" in response.headers.get("content-disposition", "")
    missing = client.get(
        f"/api/research/documents/{document_id}/source-file", headers=auth("other-user"),
    )
    assert missing.status_code == 404
