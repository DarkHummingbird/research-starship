import io
import json
import zipfile
from pathlib import Path
import pytest

from tests.research.test_p3_backup import backup_document
from tests.research.test_p2_analysis import paper


@pytest.fixture(autouse=True)
def isolate_background_jobs(monkeypatch):
    from app.research import batches
    class NoRun:
        def submit(self, *args): pass
    monkeypatch.setattr(batches, '_executor_pool', lambda: NoRun())


def test_full_restore_preview_confirmation_and_search(client, backup_document, auth, tmp_path, monkeypatch):
    _, headers, project_id, document = backup_document
    export = client.post('/api/research/export', headers=headers, json={'confirmed': True}).json()['data']
    backup = Path(export['path']).read_bytes()
    download = client.get(export['download_url'], headers=headers)
    assert download.status_code == 200 and download.content == backup
    occupied = client.post('/api/research/restore/preview', headers=headers, files={'file': ('backup.zip', backup, 'application/zip')})
    assert occupied.status_code == 200 and occupied.json()['data']['can_restore'] is False
    data = occupied.json()['data']
    blocked = client.post('/api/research/restore/confirm', headers=headers, json={'token': data['token'], 'sha256': data['sha256'], 'confirmed': True})
    assert blocked.status_code == 409
    assert client.get(f'/api/research/projects/{project_id}/documents', headers=headers).status_code == 200

    monkeypatch.setenv('RESEARCH_DATA_DIR', str(tmp_path / 'empty-restore'))
    new_headers = auth('new-restore-user')
    preview = client.post('/api/research/restore/preview', headers=new_headers, files={'file': ('backup.zip', backup, 'application/zip')})
    assert preview.status_code == 200, preview.text
    data = preview.json()['data']
    assert data['can_restore'] and data['document_count'] == 1 and data['memory_count'] > 0
    assert client.post('/api/research/restore/confirm', headers=new_headers, json={'token': data['token'], 'sha256': data['sha256'], 'confirmed': False}).status_code == 409
    restored = client.post('/api/research/restore/confirm', headers=new_headers, json={'token': data['token'], 'sha256': data['sha256'], 'confirmed': True})
    assert restored.status_code == 201, restored.text
    detail = client.get(f"/api/research/documents/{document['id']}", headers=new_headers)
    assert detail.status_code == 200 and detail.json()['data']['status'] == 'approved'
    assert client.get(f"/api/research/documents/{document['id']}/source-file", headers=new_headers).status_code == 200
    answer = client.post(f'/api/research/projects/{project_id}/chat', headers=new_headers,
                         json={'message': '保密内容仅本地保存', 'provider': 'local', 'query_confidentiality': 'L3'})
    assert answer.status_code == 200 and answer.json()['data']['sources']
    assert client.post('/api/research/restore/confirm', headers=new_headers, json={'token': data['token'], 'sha256': data['sha256'], 'confirmed': True}).status_code in (404, 409)


def test_restore_rejects_tampering_and_old_backup(client, backup_document, auth, tmp_path, monkeypatch):
    _, headers, _, _ = backup_document
    export = client.post('/api/research/export', headers=headers, json={'confirmed': True}).json()['data']
    raw = Path(export['path']).read_bytes()
    with zipfile.ZipFile(io.BytesIO(raw)) as source:
        entries = {item.filename: source.read(item) for item in source.infolist()}
    member = next(key for key in entries if key.startswith('files/'))
    entries[member] = b'tampered content'
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as target:
        for key, value in entries.items(): target.writestr(key, value)
    monkeypatch.setenv('RESEARCH_DATA_DIR', str(tmp_path / 'empty'))
    headers = auth('restore-tamper-user')
    assert client.post('/api/research/restore/preview', headers=headers, files={'file': ('bad.zip', buffer.getvalue(), 'application/zip')}).status_code == 422
    entries['manifest.json'] = json.dumps({'format': 'research-starship-backup/v1'}).encode()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as target:
        for key, value in entries.items(): target.writestr(key, value)
    assert client.post('/api/research/restore/preview', headers=headers, files={'file': ('old.zip', buffer.getvalue(), 'application/zip')}).status_code == 422


def test_restore_rejects_confidentiality_downgrade(client, backup_document, auth, tmp_path, monkeypatch):
    _, headers, _, _ = backup_document
    exported = client.post('/api/research/export', headers=headers, json={'confirmed': True}).json()['data']
    with zipfile.ZipFile(exported['path']) as source:
        entries = {item.filename: source.read(item) for item in source.infolist()}
    payload = json.loads(entries['research.json'])
    payload['chunks'][0]['confidentiality'] = 'L1'
    entries['research.json'] = json.dumps(payload).encode()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as target:
        for name, value in entries.items(): target.writestr(name, value)
    monkeypatch.setenv('RESEARCH_DATA_DIR', str(tmp_path / 'restore-invalid-level'))
    response = client.post('/api/research/restore/preview', headers=auth('restore-level-user'),
                           files={'file': ('backup.zip', buffer.getvalue(), 'application/zip')})
    assert response.status_code == 422


def test_restore_pdf_retains_source_highlight(client, paper, auth, tmp_path, monkeypatch):
    _, headers, _, document = paper
    exported = client.post('/api/research/export', headers=headers, json={'confirmed': True}).json()['data']
    backup = Path(exported['path']).read_bytes()
    monkeypatch.setenv('RESEARCH_DATA_DIR', str(tmp_path / 'pdf-restore'))
    headers = auth('pdf-restore-user')
    preview = client.post('/api/research/restore/preview', headers=headers, files={'file': ('backup.zip', backup, 'application/zip')}).json()['data']
    result = client.post('/api/research/restore/confirm', headers=headers, json={'token': preview['token'], 'sha256': preview['sha256'], 'confirmed': True})
    assert result.status_code == 201, result.text
    result = client.post(f"/api/research/documents/{document['id']}/highlight", headers=headers,
                         json={'expected_version': document['version'], 'chunk_id': document['sources'][0]['id'], 'quote': 'Baseline: KeywordSearch.'})
    assert result.status_code == 200, result.text


def test_restore_id_collision_does_not_touch_existing_data(client, backup_document, auth):
    _, headers, project_id, _ = backup_document
    exported = client.post('/api/research/export', headers=headers, json={'confirmed': True}).json()['data']
    backup = Path(exported['path']).read_bytes()
    other = auth('another-empty-user')
    preview = client.post('/api/research/restore/preview', headers=other, files={'file': ('backup.zip', backup, 'application/zip')}).json()['data']
    assert not preview['can_restore']
    result = client.post('/api/research/restore/confirm', headers=other, json={'token': preview['token'], 'sha256': preview['sha256'], 'confirmed': True})
    assert result.status_code == 409, result.text
    assert result.json()['detail']['code'] == 'restore_id_conflict'
    assert client.get(f'/api/research/projects/{project_id}/documents', headers=headers).status_code == 200
    assert client.get('/api/research/projects', headers=other).json()['data']['items'] == []


def test_restore_does_not_resume_tasks_or_external_approvals(client, backup_document, auth, tmp_path, monkeypatch):
    from app.data_access.research import memory_session
    from app.research import gateway, store
    uid, headers, project_id, document = backup_document
    approval = gateway.prepare(uid, project_id, task_id='restore-approval-test', provider='test', model_id='test',
                               destination='https://example.test', purpose='synthetic', operation='qa',
                               fragments=[{'document_id': None, 'version': 0, 'chunk_id': 'query', 'chapter': '测试',
                                           'snippet': 'test', 'classification': 'L2', 'sha256': 'a' * 64}])
    gateway.decide(uid, approval['id'], True)
    now = store.utc_now()
    with memory_session() as conn:
        conn.add(store.ResearchBatch(id='bat-restore-sample', user_id=uid, project_id=project_id, status='processing', item_count=1, created_at=now, completed_at=''))
        conn.add(store.ResearchBatchItem(id='bti-restore-sample', batch_id='bat-restore-sample', user_id=uid, project_id=project_id, document_id=document['id'], status='processing', error='', created_at=now, started_at=now, completed_at=''))
        conn.add(store.ResearchAsyncTask(id='tsk-restore-external', user_id=uid, project_id=project_id, kind='chat', status='processing', request_json='{"provider":"external"}', result_json='{}', error='', created_at=now, started_at=now, completed_at=''))
    exported = client.post('/api/research/export', headers=headers, json={'confirmed': True}).json()['data']
    backup = Path(exported['path']).read_bytes()
    monkeypatch.setenv('RESEARCH_DATA_DIR', str(tmp_path / 'restore-paused'))
    headers = auth('restore-paused-user')
    preview = client.post('/api/research/restore/preview', headers=headers, files={'file': ('backup.zip', backup, 'application/zip')}).json()['data']
    restored = client.post('/api/research/restore/confirm', headers=headers, json={'token': preview['token'], 'sha256': preview['sha256'], 'confirmed': True})
    assert restored.status_code == 201, restored.text
    tasks = client.get('/api/research/tasks', headers=headers).json()['data']['items']
    restored_task = next(task for task in tasks if task['id'] == 'bat-restore-sample')
    assert restored_task['items'][0]['status'] == 'failed'
    restored_external = client.get('/api/research/async-tasks/tsk-restore-external', headers=headers).json()['data']
    assert restored_external['status'] == 'unknown'
    assert client.post('/api/research/async-tasks/tsk-restore-external/retry', headers=headers).status_code == 409
    assert client.get(f"/api/research/outbound/{approval['id']}", headers=headers).json()['data']['status'] == 'invalidated'
    assert client.post(f"/api/research/outbound/{approval['id']}/decision", headers=headers, json={'approved': True}).status_code == 409


def test_restore_preserves_history_and_can_restore_an_old_version(client, backup_document, auth, tmp_path, monkeypatch):
    _, headers, _, document = backup_document
    changed = client.patch(f"/api/research/documents/{document['id']}", headers=headers,
                           json={'expected_version': 1, 'title': '新版本标题'})
    assert changed.status_code == 200, changed.text
    exported = client.post('/api/research/export', headers=headers, json={'confirmed': True}).json()['data']
    backup = Path(exported['path']).read_bytes()
    monkeypatch.setenv('RESEARCH_DATA_DIR', str(tmp_path / 'restore-history'))
    headers = auth('restore-history-user')
    preview = client.post('/api/research/restore/preview', headers=headers, files={'file': ('backup.zip', backup, 'application/zip')}).json()['data']
    restored = client.post('/api/research/restore/confirm', headers=headers, json={'token': preview['token'], 'sha256': preview['sha256'], 'confirmed': True})
    assert restored.status_code == 201, restored.text
    versions = client.get(f"/api/research/documents/{document['id']}/versions", headers=headers).json()['data']['items']
    assert any(version['version'] == 1 for version in versions)
    rolled_back = client.post(f"/api/research/documents/{document['id']}/restore-version", headers=headers,
                              json={'target_version': 1, 'expected_version': 2, 'confirmed': True})
    assert rolled_back.status_code == 200, rolled_back.text
    assert rolled_back.json()['data']['status'] == 'pending'


@pytest.mark.parametrize('real_embedding', [False, True])
def test_restore_schedules_missing_index_for_offline_rebuild(client, backup_document, auth, tmp_path, monkeypatch, real_embedding):
    from app.research import batches, ingest
    from app.research.local_components import component_status
    if real_embedding and not component_status()['embedding']['available']:
        pytest.skip('local embedding assets unavailable')
    real_embed = ingest.embed_texts
    _, headers, _, document = backup_document
    exported = client.post('/api/research/export', headers=headers, json={'confirmed': True}).json()['data']
    with zipfile.ZipFile(exported['path']) as source:
        entries = {item.filename: source.read(item) for item in source.infolist()}
    payload = json.loads(entries['research.json'])
    payload['embeddings'] = []
    payload['documents'][0]['embedding_status'] = 'paused'
    entries['research.json'] = json.dumps(payload).encode()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as target:
        for name, value in entries.items(): target.writestr(name, value)
    class NoRun:
        def submit(self, *args): pass
    monkeypatch.setattr(batches, '_executor_pool', lambda: NoRun())
    monkeypatch.setenv('RESEARCH_DATA_DIR', str(tmp_path / 'restore-index'))
    headers = auth('restore-index-user')
    preview = client.post('/api/research/restore/preview', headers=headers, files={'file': ('backup.zip', buffer.getvalue(), 'application/zip')}).json()['data']
    restored = client.post('/api/research/restore/confirm', headers=headers, json={'token': preview['token'], 'sha256': preview['sha256'], 'confirmed': True})
    assert restored.status_code == 201, restored.text
    assert restored.json()['data']['reindex_scheduled'] == 1
    task = client.get('/api/research/tasks', headers=headers).json()['data']['items'][0]
    assert task['items'][0]['operation'] == 'reindex'
    assert task['items'][0]['document_version'] == document['version']
    from app.research.local_components import LocalComponentError
    def unavailable(_texts):
        raise LocalComponentError('embedding', 'synthetic offline missing model')
    monkeypatch.setattr(ingest, 'embed_texts', unavailable)
    batches._process_item(task['items'][0]['id'])
    finished = client.get(f"/api/research/batches/{task['id']}", headers=headers).json()['data']
    assert finished['items'][0]['status'] == 'failed'
    monkeypatch.setattr(ingest, 'embed_texts', real_embed if real_embedding else lambda texts: {'model': 'synthetic', 'dimensions': 1024, 'vectors': [[1.0] + [0.0] * 1023 for _ in texts]})
    assert client.post(f"/api/research/batch-items/{task['items'][0]['id']}/retry", headers=headers).status_code == 202
    batches._process_item(task['items'][0]['id'])
    finished = client.get(f"/api/research/batches/{task['id']}", headers=headers).json()['data']
    assert finished['items'][0]['status'] == 'pending_review'
    assert client.get(f"/api/research/documents/{document['id']}", headers=headers).json()['data']['embedding_status'] == 'ready'
