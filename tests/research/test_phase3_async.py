import sys
import threading
import time

import pytest
from app.research import async_tasks, batches
from app.research.task_control import TaskCancelled, check_cancelled, task_scope, request_cancel
from app.research.local_components import _run_local


@pytest.fixture
def project(client, auth, tmp_path, monkeypatch):
    from app.data_access import user_scope
    monkeypatch.setenv('RESEARCH_DATA_DIR', str(tmp_path / 'research'))
    uid = 'async-owner'; headers = auth(uid)
    with user_scope(uid):
        result = client.post('/api/research/projects', headers=headers, json={'name': '异步测试'})
        yield uid, headers, result.json()['data']['id']


def wait(client, headers, task_id, states, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = client.get(f'/api/research/async-tasks/{task_id}', headers=headers).json()['data']
        if result['status'] in states: return result
        time.sleep(.02)
    raise AssertionError(result)


def test_question_survives_refresh_and_other_user_cannot_read(client, project, auth, monkeypatch):
    import app.research.service as service
    _, headers, project_id = project
    started = threading.Event(); release = threading.Event()
    def answer(uid, pid, **body):
        started.set(); release.wait(5); check_cancelled()
        return {'status':'answered','conversation_id':'cnv-test','answer':'合成回答','sources':[], 'model':'local'}
    monkeypatch.setattr(service, 'chat', answer)
    created = client.post(f'/api/research/projects/{project_id}/chat-tasks', headers=headers,
                          json={'message':'合成问题','provider':'local'})
    assert created.status_code == 202, created.text
    task_id = created.json()['data']['id']
    assert started.wait(3)
    assert client.get(f'/api/research/async-tasks/{task_id}', headers=auth('other-async-user')).status_code == 404
    assert any(t['id'] == task_id for t in client.get('/api/research/tasks', headers=headers).json()['data']['async_items'])
    release.set()
    result = wait(client, headers, task_id, {'completed'})
    assert result['result']['answer'] == '合成回答'
    assert client.get(f'/api/research/async-tasks/{task_id}', headers=headers).json()['data']['result']['answer'] == '合成回答'


def test_cancel_running_question_is_acknowledged_only_after_stop(client, project, monkeypatch):
    import app.research.service as service
    _, headers, project_id = project
    started = threading.Event()
    def slow(uid, pid, **body):
        started.set()
        while True:
            check_cancelled(); time.sleep(.02)
    monkeypatch.setattr(service, 'chat', slow)
    task_id = client.post(f'/api/research/projects/{project_id}/chat-tasks', headers=headers,
                          json={'message':'慢问题','provider':'local'}).json()['data']['id']
    assert started.wait(3)
    cancelled = client.post(f'/api/research/async-tasks/{task_id}/cancel', headers=headers)
    assert cancelled.status_code == 200, cancelled.text
    result = wait(client, headers, task_id, {'cancelled'})
    assert result['result'] == {}


def test_cancel_terminates_local_subprocess(tmp_path):
    started = threading.Event(); finished = threading.Event(); errors = []
    marker = tmp_path / 'started'
    def worker():
        try:
            with task_scope('controlled-process'):
                started.set()
                _run_local([sys.executable, '-c', 'import pathlib,sys,time; pathlib.Path(sys.argv[1]).write_text("running"); time.sleep(30)', str(marker)], timeout=40)
        except TaskCancelled:
            errors.append('cancelled')
        finally:
            finished.set()
    thread = threading.Thread(target=worker); thread.start()
    assert started.wait(2)
    deadline = time.monotonic() + 3
    while not marker.exists() and time.monotonic() < deadline: time.sleep(.02)
    assert marker.exists()
    assert request_cancel('controlled-process')
    assert finished.wait(5)
    thread.join()
    assert errors == ['cancelled']


def test_external_inflight_is_not_reported_cancelled(client, project, monkeypatch):
    import app.research.service as service
    _, headers, project_id = project
    started = threading.Event(); release = threading.Event()
    def external(uid, pid, **body):
        started.set(); release.wait(5)
        return {'status':'answered','conversation_id':'cnv-test','answer':'结果','sources':[], 'model':'external'}
    monkeypatch.setattr(service, 'chat', external)
    task_id = client.post(f'/api/research/projects/{project_id}/chat-tasks', headers=headers,
                          json={'message':'公开问题','provider':'external'}).json()['data']['id']
    assert started.wait(3)
    result = client.post(f'/api/research/async-tasks/{task_id}/cancel', headers=headers)
    assert result.status_code == 409 and result.json()['detail']['code'] == 'external_outcome_uncertain'
    release.set(); wait(client, headers, task_id, {'completed'})


def test_external_approval_survives_refresh_and_is_used_once(client, project, monkeypatch):
    import app.research.service as service
    from app.research import gateway
    _, headers, project_id = project
    upload = client.post(f'/api/research/projects/{project_id}/documents', headers=headers,
                         files={'file': ('source.md', b'# Source\n\nPublic evidence about retrieval.', 'text/markdown')},
                         data={'document_type': 'tutorial', 'confidentiality': 'L2'})
    doc = upload.json()['data']; source = doc['sources'][0]
    assert client.post(f"/api/research/documents/{doc['id']}/review", headers=headers, json={'expected_version': 1, 'approved': True}).status_code == 200
    match = {'document_id':doc['id'], 'document_version':1, 'chunk_id':source['id'], 'title':doc['title'],
             'chapter':source['chapter'], 'confidentiality':'L2', 'project_id':project_id,
             'content':source['content'], 'locator':source['locator']}
    monkeypatch.setattr(service, 'search_confirmed', lambda *a, **k: [match])
    monkeypatch.setattr(service, '_model_destination', lambda _: ('test','https://example.test'))
    sends = []
    monkeypatch.setattr(gateway, 'configured_model_sender', lambda approval: sends.append(approval['id']) or 'Synthetic answer')
    task = client.post(f'/api/research/projects/{project_id}/chat-tasks', headers=headers,
                       json={'message':'retrieval','provider':'external','model_id':'test','query_confidentiality':'L2'}).json()['data']
    task_id = task['id']
    pending = wait(client, headers, task_id, {'awaiting_approval','failed'})
    assert pending['status'] == 'awaiting_approval', pending
    approval = pending['result']['approval']
    assert not sends
    assert client.post(f'/api/research/async-tasks/{task_id}/resume', headers=headers).status_code == 409
    assert client.post(f"/api/research/outbound/{approval['id']}/decision", headers=headers, json={'approved':True}).status_code == 200
    assert client.post(f'/api/research/async-tasks/{task_id}/resume', headers=headers).status_code == 202
    result = wait(client, headers, task_id, {'completed','failed'})
    assert result['status'] == 'completed', result
    assert len(sends) == 1
    assert client.post(f'/api/research/async-tasks/{task_id}/resume', headers=headers).status_code == 409
    assert len(sends) == 1


def test_running_batch_cancel_and_version_preserved(client, project, monkeypatch):
    _, headers, project_id = project
    uploaded = client.post(f'/api/research/projects/{project_id}/documents', headers=headers,
                           files={'file': ('one.md', b'# synthetic\n\nEvidence only.', 'text/markdown')},
                           data={'document_type':'tutorial', 'confidentiality':'L3'}).json()['data']
    started = threading.Event()
    def slow(uid, doc_id, version, operation):
        started.set()
        while True: check_cancelled(); time.sleep(.02)
    monkeypatch.setattr(batches, '_run_document', slow)
    batch = client.post(f'/api/research/projects/{project_id}/batches', headers=headers, json={'document_ids':[uploaded['id']]}).json()['data']
    assert started.wait(3)
    item_id = batch['items'][0]['id']
    response = client.post(f'/api/research/batch-items/{item_id}/cancel', headers=headers)
    assert response.status_code == 200, response.text
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        status = client.get(f"/api/research/batches/{batch['id']}", headers=headers).json()['data']
        if status['items'][0]['status'] == 'cancelled': break
        time.sleep(.02)
    assert status['items'][0]['status'] == 'cancelled'
    current = client.get(f"/api/research/documents/{uploaded['id']}", headers=headers).json()['data']
    assert current['version'] == uploaded['version'] and current['status'] == 'pending'


def test_generation_result_is_persistent_and_restart_external_is_unknown(client, project, monkeypatch):
    import app.research.generations as generations
    from app.data_access.research import memory_session
    from app.research import store
    uid, headers, project_id = project
    monkeypatch.setattr(generations, 'generate_output', lambda *a, **k: {'id':'gen-test', 'content':'测试草稿', 'status':'pending'})
    created = client.post(f'/api/research/projects/{project_id}/generation-tasks', headers=headers,
                          json={'kind':'review','query':'测试','document_ids':[]})
    assert created.status_code == 202
    done = wait(client, headers, created.json()['data']['id'], {'completed'})
    assert done['result']['content'] == '测试草稿'
    now = store.utc_now()
    with memory_session() as conn:
        conn.add(store.ResearchAsyncTask(id='tsk-interrupted-external', user_id=uid, project_id=project_id,
            kind='chat', status='processing', request_json='{"provider":"external"}', result_json='{}', error='',
            created_at=now, started_at=now, completed_at=''))
    assert async_tasks.recover() == 1
    result = client.get('/api/research/async-tasks/tsk-interrupted-external', headers=headers).json()['data']
    assert result['status'] == 'unknown'
    assert client.post('/api/research/async-tasks/tsk-interrupted-external/retry', headers=headers).status_code == 409


def test_question_does_not_publish_answer_after_source_version_changes(client, project, monkeypatch):
    import app.research.service as service
    import app.research.rag as rag
    _, headers, project_id = project
    doc = client.post(f'/api/research/projects/{project_id}/documents', headers=headers,
        files={'file':('source.md', b'# Source\n\nSynthetic evidence about results.', 'text/markdown')},
        data={'document_type':'tutorial','confidentiality':'L2'}).json()['data']
    assert client.post(f"/api/research/documents/{doc['id']}/review", headers=headers, json={'expected_version':1,'approved':True}).status_code == 200
    source=doc['sources'][0]
    match={'document_id':doc['id'],'document_version':1,'chunk_id':source['id'],'title':doc['title'],
           'chapter':source['chapter'],'confidentiality':'L2','project_id':project_id,'content':source['content'],'locator':source['locator']}
    monkeypatch.setattr(service, 'search_confirmed', lambda *a,**k:[match])
    started=threading.Event(); release=threading.Event()
    def slow(*args):
        started.set(); release.wait(5); return {'text':'Old source answer','model':'synthetic'}
    monkeypatch.setattr(rag, 'answer', slow)
    task = client.post(f'/api/research/projects/{project_id}/chat-tasks', headers=headers,
                       json={'message':'results','provider':'rag'}).json()['data']
    assert started.wait(3)
    changed=client.patch(f"/api/research/documents/{doc['id']}", headers=headers, json={'expected_version':1,'title':'Changed'})
    assert changed.status_code == 200
    release.set()
    result=wait(client, headers, task['id'], {'failed','completed'})
    assert result['status'] == 'failed'
    assert result['result']['code'] == 'document_version_changed'


def test_external_timeout_after_send_is_unknown_and_not_retryable(client, project, monkeypatch):
    import app.research.service as service
    from app.research import gateway
    _, headers, project_id = project
    doc = client.post(f'/api/research/projects/{project_id}/documents', headers=headers,
        files={'file':('source.md', b'# Source\n\nSynthetic retrieval evidence.', 'text/markdown')},
        data={'document_type':'tutorial','confidentiality':'L2'}).json()['data']
    client.post(f"/api/research/documents/{doc['id']}/review", headers=headers, json={'expected_version':1,'approved':True})
    source=doc['sources'][0]
    match={'document_id':doc['id'],'document_version':1,'chunk_id':source['id'],'title':doc['title'],
           'chapter':source['chapter'],'confidentiality':'L2','project_id':project_id,'content':source['content'],'locator':source['locator']}
    monkeypatch.setattr(service,'search_confirmed',lambda *a,**k:[match])
    monkeypatch.setattr(service,'_model_destination',lambda _:('test','https://example.test'))
    calls=[]
    def timeout(_approval):
        calls.append('sent'); raise TimeoutError('synthetic timeout')
    monkeypatch.setattr(gateway,'configured_model_sender',timeout)
    task=client.post(f'/api/research/projects/{project_id}/chat-tasks',headers=headers,
                     json={'message':'retrieval','provider':'external','model_id':'test','query_confidentiality':'L2'}).json()['data']
    pending=wait(client,headers,task['id'],{'awaiting_approval','failed'})
    approval=pending['result']['approval']
    client.post(f"/api/research/outbound/{approval['id']}/decision",headers=headers,json={'approved':True})
    client.post(f"/api/research/async-tasks/{task['id']}/resume",headers=headers)
    result=wait(client,headers,task['id'],{'unknown','failed','completed'})
    assert result['status'] == 'unknown' and calls == ['sent']
    assert client.post(f"/api/research/async-tasks/{task['id']}/retry",headers=headers).status_code == 409
    assert calls == ['sent']


def test_sent_external_answer_with_changed_source_is_not_safe_to_retry(client, project, monkeypatch):
    import app.research.service as service
    from app.research import gateway
    _, headers, project_id = project
    doc = client.post(f'/api/research/projects/{project_id}/documents', headers=headers,
        files={'file':('source.md', b'# Source\n\nSynthetic evidence.', 'text/markdown')},
        data={'document_type':'tutorial','confidentiality':'L2'}).json()['data']
    client.post(f"/api/research/documents/{doc['id']}/review", headers=headers, json={'expected_version':1,'approved':True})
    source=doc['sources'][0]
    match={'document_id':doc['id'],'document_version':1,'chunk_id':source['id'],'title':doc['title'],
           'chapter':source['chapter'],'confidentiality':'L2','project_id':project_id,'content':source['content'],'locator':source['locator']}
    monkeypatch.setattr(service,'search_confirmed',lambda *a,**k:[match])
    monkeypatch.setattr(service,'_model_destination',lambda _:('test','https://example.test'))
    calls=[]
    def sender(_approval):
        calls.append('sent')
        changed=client.patch(f"/api/research/documents/{doc['id']}",headers=headers,json={'expected_version':1,'title':'changed after sending'})
        assert changed.status_code == 200
        return 'Synthetic answer'
    monkeypatch.setattr(gateway,'configured_model_sender',sender)
    task=client.post(f'/api/research/projects/{project_id}/chat-tasks',headers=headers,
        json={'message':'evidence','provider':'external','model_id':'test','query_confidentiality':'L2'}).json()['data']
    pending=wait(client,headers,task['id'],{'awaiting_approval','failed'})
    approval=pending['result']['approval']
    client.post(f"/api/research/outbound/{approval['id']}/decision",headers=headers,json={'approved':True})
    client.post(f"/api/research/async-tasks/{task['id']}/resume",headers=headers)
    result=wait(client,headers,task['id'],{'unknown','failed','completed'})
    assert result['status'] == 'unknown' and calls == ['sent']
    assert client.post(f"/api/research/async-tasks/{task['id']}/retry",headers=headers).status_code == 409


def test_cancel_queued_approved_external_task_invalidates_approval(client, project, monkeypatch):
    import app.research.service as service
    from app.research import gateway
    _, headers, project_id = project
    doc = client.post(f'/api/research/projects/{project_id}/documents', headers=headers,
        files={'file':('source.md', b'# Source\n\nSynthetic evidence.', 'text/markdown')},
        data={'document_type':'tutorial','confidentiality':'L2'}).json()['data']
    client.post(f"/api/research/documents/{doc['id']}/review", headers=headers, json={'expected_version':1,'approved':True})
    source=doc['sources'][0]
    match={'document_id':doc['id'],'document_version':1,'chunk_id':source['id'],'title':doc['title'],
           'chapter':source['chapter'],'confidentiality':'L2','project_id':project_id,'content':source['content'],'locator':source['locator']}
    monkeypatch.setattr(service,'search_confirmed',lambda *a,**k:[match])
    monkeypatch.setattr(service,'_model_destination',lambda _:('test','https://example.test'))
    calls=[]; monkeypatch.setattr(gateway,'configured_model_sender',lambda _: calls.append(1) or 'answer')
    task=client.post(f'/api/research/projects/{project_id}/chat-tasks',headers=headers,
        json={'message':'evidence','provider':'external','model_id':'test','query_confidentiality':'L2'}).json()['data']
    pending=wait(client,headers,task['id'],{'awaiting_approval','failed'})
    approval=pending['result']['approval']
    client.post(f"/api/research/outbound/{approval['id']}/decision",headers=headers,json={'approved':True})
    class NoRun:
        def submit(self,*args): pass
    monkeypatch.setattr(async_tasks,'_pool',NoRun())
    assert client.post(f"/api/research/async-tasks/{task['id']}/resume",headers=headers).status_code == 202
    assert client.post(f"/api/research/async-tasks/{task['id']}/cancel",headers=headers).status_code == 200
    assert client.get(f"/api/research/outbound/{approval['id']}",headers=headers).json()['data']['status'] == 'invalidated'
    assert calls == []
