from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

import pytest

from app.research import batches, store


@pytest.fixture
def batch_project(client, auth, tmp_path, monkeypatch):
    from app.data_access import user_scope

    monkeypatch.setenv("RESEARCH_DATA_DIR", str(tmp_path / "research"))
    uid = "p3-batch-user"
    headers = auth(uid)
    with user_scope(uid):
        project = client.post("/api/research/projects", json={"name": "批次处理"}, headers=headers)
        yield uid, headers, project.json()["data"]["id"]


def add_document(client, headers, project_id, index):
    response = client.post(
        f"/api/research/projects/{project_id}/documents",
        files={"file": (f"paper-{index}.md", f"# 合成论文 {index}\n\n方法 {index} 与来源。".encode(), "text/markdown")},
        data={"document_type": "tutorial", "confidentiality": "L1"}, headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


def wait_done(client, headers, batch_id, timeout=15):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        result=client.get(f"/api/research/batches/{batch_id}",headers=headers).json()["data"]
        if result["status"].startswith("completed"):
            return result
        time.sleep(.05)
    raise AssertionError(f"batch did not finish: {result}")


def test_five_items_two_workers_one_failure_and_manual_retry(client, batch_project, monkeypatch):
    _, headers, project_id = batch_project
    documents=[add_document(client,headers,project_id,index) for index in range(5)]
    active=0
    maximum=0
    lock=threading.Lock()
    original=batches._run_document

    def controlled(uid,document_id,version=0,operation="analysis"):
        nonlocal active,maximum
        with lock:
            active+=1
            maximum=max(maximum,active)
        time.sleep(.15)
        try:
            if document_id==documents[2]["id"]:
                raise RuntimeError("synthetic single failure")
            original(uid,document_id,version,operation)
        finally:
            with lock:
                active-=1

    monkeypatch.setattr(batches,"_run_document",controlled)
    response=client.post(f"/api/research/projects/{project_id}/batches",json={"document_ids":[d["id"] for d in documents]},headers=headers)
    assert response.status_code==202,response.text
    result=wait_done(client,headers,response.json()["data"]["id"])
    assert maximum==2
    assert result["summary"]["pending_review"]==4
    assert result["summary"]["failed"]==1
    assert result["status"]=="completed_with_errors"
    failed=next(item for item in result["items"] if item["status"]=="failed")
    monkeypatch.setattr(batches,"_run_document",original)
    response=client.post(f"/api/research/batch-items/{failed['id']}/retry",headers=headers)
    assert response.status_code==202,response.text
    result=wait_done(client,headers,result["id"])
    assert result["status"]=="completed"
    assert result["summary"]["pending_review"]==5

    sixth=add_document(client,headers,project_id,6)
    response=client.post(f"/api/research/projects/{project_id}/batches",json={"document_ids":[d["id"] for d in documents]+[sixth["id"]]},headers=headers)
    assert response.status_code==409
    assert response.json()["detail"]["code"]=="batch_split_required"


def test_interrupted_processing_requires_manual_retry(batch_project):
    uid,_,project_id=batch_project
    now=store.utc_now()
    from app.data_access.research import memory_session,migration_session
    with memory_session() as conn:
        batch=store.ResearchBatch(id="bat-interrupted",user_id=uid,project_id=project_id,status="processing",item_count=1,created_at=now,completed_at="")
        conn.add(batch)
        conn.add(store.ResearchBatchItem(id="bti-interrupted",batch_id=batch.id,user_id=uid,project_id=project_id,document_id="doc-missing",status="processing",error="",created_at=now,started_at=now,completed_at=""))
    assert batches.recover_interrupted_batches()==1
    with migration_session() as conn:
        item=conn.get(store.ResearchBatchItem,"bti-interrupted")
        assert item.status=="failed"
        assert "手动重试" in item.error


def test_task_list_and_cancel_queued_are_owner_scoped(client, batch_project, auth):
    uid, headers, project_id = batch_project
    now = store.utc_now()
    from app.data_access.research import memory_session
    with memory_session() as conn:
        conn.add(store.ResearchBatch(id="bat-queued", user_id=uid, project_id=project_id, status="queued", item_count=1, created_at=now, completed_at=""))
        conn.add(store.ResearchBatchItem(id="bti-queued", batch_id="bat-queued", user_id=uid, project_id=project_id, document_id="missing", status="queued", error="", created_at=now, started_at="", completed_at=""))
    assert client.get('/api/research/tasks', headers=headers).json()['data']['items'][0]['summary']['queued'] == 1
    assert client.post('/api/research/batch-items/bti-queued/cancel', headers=auth('other-task-owner')).status_code == 404
    cancelled = client.post('/api/research/batch-items/bti-queued/cancel', headers=headers)
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()['data']['status'] == 'cancelled'
    assert client.post('/api/research/batch-items/bti-queued/cancel', headers=headers).status_code == 409


def test_restart_marks_queued_as_interrupted(batch_project):
    uid, _, project_id = batch_project
    from app.data_access.research import memory_session
    now = store.utc_now()
    with memory_session() as conn:
        conn.add(store.ResearchBatch(id='bat-restart-queued', user_id=uid, project_id=project_id, status='queued', item_count=1, created_at=now, completed_at=''))
        conn.add(store.ResearchBatchItem(id='bti-restart-queued', batch_id='bat-restart-queued', user_id=uid, project_id=project_id, document_id='missing', status='queued', error='', created_at=now, started_at='', completed_at=''))
    assert batches.recover_interrupted_batches() == 1
    assert batches.get_batch(uid, 'bat-restart-queued')['items'][0]['status'] == 'failed'


def test_queued_task_rejects_document_version_change(client, batch_project, monkeypatch):
    _, headers, project_id = batch_project
    document = add_document(client, headers, project_id, 11)
    class NoRun:
        def submit(self, *args): pass
    monkeypatch.setattr(batches, '_executor_pool', lambda: NoRun())
    result = client.post(f'/api/research/projects/{project_id}/batches', headers=headers, json={'document_ids': [document['id']]}).json()['data']
    item = result['items'][0]
    assert item['document_version'] == 1
    changed = client.patch(f"/api/research/documents/{document['id']}", headers=headers, json={'expected_version': 1, 'title': '新版标题'})
    assert changed.status_code == 200, changed.text
    from app.research.ingest import index_document
    assert index_document(batch_project[0], document['id'], expected_version=1)['embedding_status'] == 'stale'
    batches._process_item(item['id'])
    result = client.get(f"/api/research/batches/{result['id']}", headers=headers).json()['data']
    assert result['items'][0]['status'] == 'failed'
    assert '版本已变化' in result['items'][0]['error']
