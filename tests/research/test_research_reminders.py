from __future__ import annotations

import time

from app.agent.proactive.store import list_pending
from app.data_access import user_scope
from app.research import batches


def test_pending_review_document_creates_reminder_item(client, auth, monkeypatch):
    headers = auth("research-reminder-user")
    project = client.post("/api/research/projects", json={"name": "提醒"}, headers=headers).json()["data"]
    document = client.post(
        f"/api/research/projects/{project['id']}/documents",
        files={"file": ("note.md", "# 说明\n\n待确认的内容。".encode(), "text/markdown")},
        data={"confidentiality": "L1", "document_type": "tutorial"}, headers=headers,
    ).json()["data"]

    monkeypatch.setattr(batches, "_run_document", lambda *args, **kwargs: {})
    created = client.post(
        f"/api/research/projects/{project['id']}/batches",
        json={"document_ids": [document["id"]]}, headers=headers,
    )
    assert created.status_code == 202, created.text
    batch_id = created.json()["data"]["id"]
    deadline = time.time() + 15
    while time.time() < deadline:
        current = client.get(f"/api/research/batches/{batch_id}", headers=headers).json()["data"]
        if current["summary"]["pending_review"] == 1:
            break
        time.sleep(0.05)
    else:
        raise AssertionError("batch did not reach pending_review")

    with user_scope("research-reminder-user"):
        items = list_pending("research-reminder-user")
    matching = [item for item in items if item["kind"] == "research_review" and item["payload"].get("document_id") == document["id"]]
    assert matching
    assert "待我确认" in matching[0]["body"]