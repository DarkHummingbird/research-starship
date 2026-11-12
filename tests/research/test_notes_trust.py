def test_note_provenance_invalidated_and_reviewed_across_source_versions(client, auth, tmp_path, monkeypatch):
    monkeypatch.setenv("RESEARCH_DATA_DIR", str(tmp_path / "research"))
    headers = auth("note-trust-user")
    project = client.post("/api/research/projects", json={"name": "信任链"}, headers=headers).json()["data"]
    source = client.post(
        f"/api/research/projects/{project['id']}/documents",
        files={"file": ("source.md", "# 来源\n\n只有确认后的资料才能进入课题检索。", "text/markdown")},
        data={"confidentiality": "L2", "document_type": "tutorial"}, headers=headers,
    ).json()["data"]
    client.post(
        f"/api/research/documents/{source['id']}/review",
        json={"expected_version": source["version"], "approved": True}, headers=headers,
    )
    source_chunk = source["sources"][0]
    note = client.post(
        f"/api/research/projects/{project['id']}/study-notes",
        json={
            "title": "入库规则笔记", "content": "确认后的资料才进入检索。", "document_type": "research_note",
            "note_type": "ai_summary", "confidentiality": "L1",
            "model_info": {"model": "synthetic-generator", "task_id": "synthetic-task"},
            "source_refs": [{
                "document_id": source["id"], "version": source["version"], "chunk_id": source_chunk["id"],
                "quote": source_chunk["content"], "locator": source_chunk.get("locator") or {},
            }],
        }, headers=headers,
    )
    assert note.status_code == 201, note.text
    note_data = note.json()["data"]
    assert note_data["confidentiality"] == "L2"
    assert note_data["parsed"]["note_type"] == "ai_summary"
    assert note_data["parsed"]["note_provenance"][0]["version"] == source["version"]

    approved = client.post(
        f"/api/research/documents/{note_data['id']}/review",
        json={"expected_version": note_data["version"], "approved": True}, headers=headers,
    )
    assert approved.status_code == 200, approved.text

    updated = client.patch(
        f"/api/research/documents/{source['id']}",
        json={"expected_version": source["version"], "title": "来源第二版"}, headers=headers,
    )
    assert updated.status_code == 200, updated.text
    documents = client.get(f"/api/research/projects/{project['id']}/documents", headers=headers).json()["data"]["items"]
    stored_note = next(item for item in documents if item["id"] == note_data["id"])
    assert stored_note["status"] == "pending"
    assert stored_note["parsed"]["note_validity"] == "source_changed"

    source_current = client.get(f"/api/research/documents/{source['id']}", headers=headers).json()["data"]
    client.post(
        f"/api/research/documents/{source['id']}/review",
        json={"expected_version": source_current["version"], "approved": True}, headers=headers,
    )
    reapproved = client.post(
        f"/api/research/documents/{note_data['id']}/review",
        json={"expected_version": note_data["version"], "approved": True}, headers=headers,
    )
    assert reapproved.status_code == 200, reapproved.text
    refreshed = reapproved.json()["data"]["parsed"]["note_provenance"][0]
    assert refreshed["version"] == source_current["version"]

    deleted = client.post(
        f"/api/research/documents/{source['id']}/delete",
        json={"expected_version": source_current["version"], "confirmed": True}, headers=headers,
    )
    assert deleted.status_code == 200
    final_note = client.get(f"/api/research/documents/{note_data['id']}", headers=headers).json()["data"]
    assert final_note["status"] == "pending"
    assert final_note["parsed"]["note_validity"] == "source_withdrawn"


def test_ai_summary_requires_source_but_user_record_does_not(client, auth, tmp_path, monkeypatch):
    monkeypatch.setenv("RESEARCH_DATA_DIR", str(tmp_path / "research"))
    headers = auth("note-source-required")
    project = client.post("/api/research/projects", json={"name": "来源规则"}, headers=headers).json()["data"]
    missing = client.post(
        f"/api/research/projects/{project['id']}/study-notes",
        json={"title": "无来源归纳", "content": "模型推测", "document_type": "research_note", "note_type": "ai_summary"},
        headers=headers,
    )
    assert missing.status_code == 422
    assert missing.json()["detail"]["code"] == "note_source_required"

    user_record = client.post(
        f"/api/research/projects/{project['id']}/study-notes",
        json={"title": "我的记录", "content": "这是我自己的研究判断。", "document_type": "research_note", "note_type": "user_record"},
        headers=headers,
    )
    assert user_record.status_code == 201
    assert user_record.json()["data"]["parsed"]["note_type"] == "user_record"
