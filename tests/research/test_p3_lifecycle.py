from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from app.data_access.research import memory_session, migration_session
from app.research import store


@pytest.fixture
def lifecycle_document(client, auth, tmp_path, monkeypatch):
    from app.data_access import user_scope

    monkeypatch.setenv("RESEARCH_DATA_DIR", str(tmp_path / "research"))
    uid = "p3-lifecycle-user"
    headers = auth(uid)
    with user_scope(uid):
        project = client.post("/api/research/projects", json={"name": "生命周期"}, headers=headers)
        project_id = project.json()["data"]["id"]
        response = client.post(
            f"/api/research/projects/{project_id}/documents",
            files={"file": ("tutorial.md", "# 教程\n\n## 规则\n确认后入库。".encode(), "text/markdown")},
            data={"document_type": "tutorial", "confidentiality": "L3"}, headers=headers,
        )
        document = response.json()["data"]
        approved = client.post(
            f"/api/research/documents/{document['id']}/review",
            json={"expected_version": 1, "approved": True}, headers=headers,
        ).json()["data"]
        yield uid, headers, project_id, approved


def test_version_restore_and_recent_undo(client, lifecycle_document):
    _, headers, project_id, document = lifecycle_document
    update = client.patch(
        f"/api/research/documents/{document['id']}",
        json={"expected_version": 1, "title": "教程新标题"}, headers=headers,
    )
    assert update.status_code == 200, update.text
    updated = update.json()["data"]
    assert updated["version"] == 2 and updated["status"] == "pending"
    assert updated["change"]["operation"] == "update"
    versions = client.get(f"/api/research/documents/{document['id']}/versions", headers=headers).json()["data"]["items"]
    assert any(item["version"] == 1 for item in versions)

    restored = client.post(
        f"/api/research/documents/{document['id']}/restore-version",
        json={"target_version": 1, "expected_version": 2, "confirmed": True}, headers=headers,
    )
    assert restored.status_code == 200, restored.text
    current = restored.json()["data"]
    assert current["version"] == 3
    assert current["title"] == "教程"
    assert current["status"] == "pending"
    assert current["parsed"]["title"] == "教程"
    changes = client.get(f"/api/research/projects/{project_id}/changes", headers=headers).json()["data"]["items"]
    assert len(changes) <= 10
    update_change = next(item for item in changes if item["operation"] == "update")
    undone = client.post(f"/api/research/changes/{update_change['id']}/undo", json={"confirmed": True}, headers=headers)
    assert undone.status_code == 200, undone.text
    undone_doc = undone.json()["data"]["document"]
    assert undone_doc["version"] == 4
    assert undone_doc["title"] == "教程"
    repeated = client.post(f"/api/research/changes/{update_change['id']}/undo", json={"confirmed": True}, headers=headers)
    assert repeated.status_code == 200 and repeated.json()["data"]["idempotent"] is True


def test_recycle_restore_and_permanent_delete_preserves_original(client, lifecycle_document, tmp_path):
    uid, headers, project_id, document = lifecycle_document
    original = tmp_path / "original.md"
    original.write_text("用户原件不变", encoding="utf-8")
    original_before = original.read_bytes()
    with migration_session() as conn:
        row = conn.get(store.ResearchDocument, document["id"])
        system_copy = Path(row.stored_path)
    assert system_copy.is_file()
    response = client.post(
        f"/api/research/documents/{document['id']}/delete",
        json={"expected_version": document["version"], "confirmed": True}, headers=headers,
    )
    assert response.status_code == 200, response.text
    deleted = response.json()["data"]
    assert deleted["status"] == "deleted"
    assert deleted["change"]["operation"] == "delete"
    listed = client.get(f"/api/research/projects/{project_id}/documents", headers=headers).json()["data"]["items"]
    assert listed == []
    recycle = client.get(f"/api/research/projects/{project_id}/recycle-bin", headers=headers).json()["data"]["items"]
    assert len(recycle) == 1 and recycle[0]["permanent_delete_eligible"] is False

    restored = client.post(f"/api/research/documents/{document['id']}/restore", json={"confirmed": True}, headers=headers)
    assert restored.status_code == 200, restored.text
    assert restored.json()["data"]["status"] == "approved"
    with migration_session() as conn:
        active = conn.execute(select(store.ResearchMemory).where(
            store.ResearchMemory.document_id == document["id"], store.ResearchMemory.active.is_(True)
        )).scalars().all()
        assert active

    client.post(
        f"/api/research/documents/{document['id']}/delete",
        json={"expected_version": document["version"], "confirmed": True}, headers=headers,
    )
    with memory_session() as conn:
        row = conn.get(store.ResearchDocument, document["id"])
        row.delete_after = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    response = client.request("DELETE", f"/api/research/documents/{document['id']}/permanent", json={"confirmed": True}, headers=headers)
    assert response.status_code == 200, response.text
    result = response.json()["data"]
    assert result["permanently_deleted"] is True
    assert result["change"]["undoable"] is False
    assert not system_copy.exists()
    assert original.read_bytes() == original_before
