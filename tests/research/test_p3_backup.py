from __future__ import annotations

import hashlib
import json
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from app.data_access.research import memory_session, migration_session
from app.research import gateway, store


@pytest.fixture
def backup_document(client, auth, tmp_path, monkeypatch):
    from app.data_access import user_scope

    monkeypatch.setenv("RESEARCH_DATA_DIR", str(tmp_path / "research"))
    uid = "p3-backup-user"
    headers = auth(uid)
    with user_scope(uid):
        project = client.post("/api/research/projects", json={"name": "备份验收"}, headers=headers)
        project_id = project.json()["data"]["id"]
        response = client.post(
            f"/api/research/projects/{project_id}/documents",
            files={"file": ("paper.md", "# 合成资料\n\n## 证据\n保密内容仅本地保存。".encode(), "text/markdown")},
            data={"document_type": "tutorial", "confidentiality": "L3"}, headers=headers,
        )
        document = response.json()["data"]
        client.post(
            f"/api/research/documents/{document['id']}/review",
            json={"expected_version": 1, "approved": True}, headers=headers,
        )
        yield uid, headers, project_id, document


def test_confirmed_zip_contains_cards_originals_and_missing_manifest(client, backup_document):
    uid, headers, project_id, document = backup_document
    denied = client.post("/api/research/export", json={"confirmed": False}, headers=headers)
    assert denied.status_code == 409
    response = client.post("/api/research/export", json={"confirmed": True}, headers=headers)
    assert response.status_code == 201, response.text
    result = response.json()["data"]
    target = Path(result["path"])
    assert target.is_file()
    with zipfile.ZipFile(target) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        data = json.loads(archive.read("research.json"))
        assert manifest["confidentiality_counts"]["L3"] == 1
        assert data["documents"][0]["id"] == document["id"]
        assert f"files/{document['id']}/paper.md" in archive.namelist()
    assert result["remote_exported"] is False

    with migration_session() as conn:
        row = conn.get(store.ResearchDocument, document["id"])
        Path(row.stored_path).unlink()
    second = client.post("/api/research/export", json={"confirmed": True}, headers=headers)
    assert second.status_code == 201
    result = second.json()["data"]
    assert result["filename"] == "research-starship-backup-v2.zip"
    assert result["missing_originals"][0]["document_id"] == document["id"]
    with zipfile.ZipFile(result["path"]) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["missing_originals"]


def test_security_log_retention_is_180_days(backup_document):
    uid, _, project_id, _ = backup_document
    old = (datetime.now(timezone.utc) - timedelta(days=181)).isoformat()
    with memory_session() as conn:
        conn.add(store.ResearchSecurityEvent(
            user_id=uid, project_id=project_id, classification="L3", operation="qa",
            provider="test", purpose="synthetic", outcome="blocked_l3", content_sha256=hashlib.sha256(b"old").hexdigest(),
            snippet="synthetic old event", created_at=old,
        ))
    gateway.prepare(
        uid, project_id, task_id="retention-test", provider="deepseek", model_id="test",
        destination="https://example.test", purpose="synthetic", operation="qa",
        fragments=[{
            "document_id": None, "version": 0, "chunk_id": "query", "chapter": "测试",
            "snippet": "synthetic current", "classification": "L2", "sha256": hashlib.sha256(b"current").hexdigest(),
        }],
    )
    with migration_session() as conn:
        rows = conn.execute(select(store.ResearchSecurityEvent).where(store.ResearchSecurityEvent.user_id == uid)).scalars().all()
        assert len(rows) == 1
        assert rows[0].snippet == "synthetic current"


def test_lightweight_backup_contains_current_results_without_originals(client, backup_document):
    uid, headers, _project_id, document = backup_document
    denied = client.post("/api/research/export/light", json={"confirmed": False}, headers=headers)
    assert denied.status_code == 409
    response = client.post("/api/research/export/light", json={"confirmed": True}, headers=headers)
    assert response.status_code == 201, response.text
    result = response.json()["data"]
    assert result["includes_originals"] is False
    assert result["includes_history"] is False
    assert result["full_restore_supported"] is False
    assert result["remote_exported"] is False
    target = Path(result["path"])
    assert target.stat().st_mode & 0o777 == 0o600
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["manifest"]["format"] == "research-starship-light-backup/v1"
    assert payload["manifest"]["memory_count"] == len(payload["memories"]) > 0
    assert payload["documents"][0]["id"] == document["id"]
    assert "stored_path" not in payload["documents"][0]
    assert "source_text" not in payload["documents"][0]
    assert "raw_model_output" not in payload["documents"][0]
    assert "user_id" not in payload["projects"][0]
    downloaded = client.get(result["download_url"], headers=headers)
    assert downloaded.status_code == 200
    assert downloaded.content == target.read_bytes()
    second = client.post("/api/research/export/light", json={"confirmed": True}, headers=headers)
    assert second.json()["data"]["filename"] == "research-starship-light-backup-v2.json"
