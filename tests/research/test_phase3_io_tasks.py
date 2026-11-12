import time
from pathlib import Path

from tests.research.test_p3_backup import backup_document


def wait(client, headers, task_id):
    for _ in range(400):
        response = client.get('/api/research/async-tasks/' + task_id, headers=headers)
        task = response.json()['data']
        if task['status'] not in ('queued', 'processing', 'cancelling'):
            return task
        time.sleep(.02)
    raise AssertionError(task)


def test_upload_task_is_persistent_and_private(client, auth, tmp_path, monkeypatch):
    monkeypatch.setenv('RESEARCH_DATA_DIR', str(tmp_path / 'upload'))
    headers = auth('io-upload-user')
    project = client.post('/api/research/projects', headers=headers, json={'name':'测试课题'}).json()['data']['id']
    response = client.post(f'/api/research/projects/{project}/document-tasks', headers=headers,
                           files={'file':('tutorial.md', b'# Test\nExample tutorial content.', 'text/markdown')},
                           data={'document_type':'tutorial','confidentiality':'L2'})
    assert response.status_code == 202, response.text
    task_id = response.json()['data']['id']
    assert client.get('/api/research/async-tasks/' + task_id, headers=auth('another-io-user')).status_code == 404
    task = wait(client, headers, task_id)
    assert task['status'] == 'completed', task
    assert task['result']['status'] == 'pending'
    assert client.get('/api/research/documents/' + task['result']['id'], headers=headers).status_code == 200
    assert any(t['id'] == task_id for t in client.get('/api/research/tasks', headers=headers).json()['data']['async_items'])


def test_external_task_approval_and_no_automatic_retry(client, auth, tmp_path, monkeypatch):
    from app.research import external_sources
    monkeypatch.setenv('RESEARCH_DATA_DIR', str(tmp_path / 'source'))
    headers = auth('io-source-user')
    project = client.post('/api/research/projects', headers=headers, json={'name':'检索课题'}).json()['data']['id']
    calls = []
    def fail(*args, **kwargs):
        calls.append(kwargs)
        raise ValueError('synthetic network failure')
    monkeypatch.setattr(external_sources, '_request', fail)
    response = client.post(f'/api/research/projects/{project}/source-tasks/arxiv/search', headers=headers,
                           json={'query':'synthetic private query', 'confidentiality':'L2'})
    assert response.status_code == 202
    task_id = response.json()['data']['id']
    task = wait(client, headers, task_id)
    assert task['status'] == 'awaiting_approval', task
    assert calls == []
    approval = task['result']['approval']['id']
    assert client.post('/api/research/async-tasks/' + task_id + '/resume', headers=headers).status_code == 409
    assert client.post(f'/api/research/outbound/{approval}/decision', headers=headers, json={'approved':True}).status_code == 200
    assert client.post('/api/research/async-tasks/' + task_id + '/resume', headers=headers).status_code == 202
    task = wait(client, headers, task_id)
    assert task['status'] == 'unknown', task
    assert len(calls) == 1 and calls[0]['retries'] == 0
    assert client.post('/api/research/async-tasks/' + task_id + '/retry', headers=headers).status_code == 409


def test_restore_tasks_require_confirmation_and_empty_account(client, backup_document, auth, tmp_path, monkeypatch):
    uid, headers, _, document = backup_document
    from app.research import async_tasks

    result = async_tasks.create(uid, '', 'backup_full', {})
    exported = wait(client, headers, result['id'])
    assert exported['status'] == 'completed', exported
    raw = Path(exported['result']['path']).read_bytes()
    monkeypatch.setenv('RESEARCH_DATA_DIR', str(tmp_path / 'isolated-restore'))
    headers = auth('io-restore-user')
    result = client.post('/api/research/restore-tasks/preview', headers=headers, files={'file':('backup.zip', raw, 'application/zip')})
    assert result.status_code == 202, result.text
    preview = wait(client, headers, result.json()['data']['id'])
    assert preview['status'] == 'completed' and preview['result']['can_restore'], preview
    body = {'token':preview['result']['token'], 'sha256':preview['result']['sha256'], 'confirmed':False}
    assert client.post('/api/research/restore-tasks/confirm', headers=headers, json=body).status_code == 409
    body['confirmed'] = True
    result = client.post('/api/research/restore-tasks/confirm', headers=headers, json=body)
    assert result.status_code == 202, result.text
    restored = wait(client, headers, result.json()['data']['id'])
    assert restored['status'] == 'completed', restored
    assert client.get('/api/research/documents/' + document['id'], headers=headers).status_code == 200
    result = client.post('/api/research/restore-tasks/preview', headers=headers, files={'file':('backup.zip', raw, 'application/zip')})
    occupied = wait(client, headers, result.json()['data']['id'])
    assert occupied['result']['can_restore'] is False


def test_interrupted_network_and_import_tasks_are_not_replayed(client, auth, tmp_path, monkeypatch):
    import json
    from app.data_access.research import memory_session
    from app.research import async_tasks, store
    from app.data_access import user_scope
    monkeypatch.setenv('RESEARCH_DATA_DIR', str(tmp_path / 'recovery'))
    uid = 'io-recovery-user'
    headers = auth(uid)
    project = client.post('/api/research/projects', headers=headers, json={'name':'中断测试'}).json()['data']['id']
    now = store.utc_now()
    with user_scope(uid), memory_session() as conn:
        for kind in ('arxiv_import', 'upload', 'restore_confirm'):
            conn.add(store.ResearchAsyncTask(id='tsk-io-' + kind, user_id=uid, project_id=project if kind != 'restore_confirm' else '',
                kind=kind, status='processing', request_json=json.dumps({}), result_json='{}', error='',
                created_at=now, started_at=now, completed_at=''))
    async_tasks.recover()
    for kind in ('arxiv_import', 'upload', 'restore_confirm'):
        task_id = 'tsk-io-' + kind
        task = client.get('/api/research/async-tasks/' + task_id, headers=headers).json()['data']
        assert task['status'] == 'unknown'
        assert client.post('/api/research/async-tasks/' + task_id + '/retry', headers=headers).status_code == 409
