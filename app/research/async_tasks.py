"""Persistent local research work. External calls are never retried automatically."""
from __future__ import annotations

import json
import uuid
import hashlib
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from sqlalchemy import select, update
from app.data_access.research import memory_session, migration_session
from app.research import store
from app.research.task_control import TaskCancelled, check_cancelled, request_cancel, task_scope

NETWORK_KINDS = {'arxiv_search', 'arxiv_import', 'github_import', 'webpage_import'}
GLOBAL_KINDS = {'restore_preview', 'restore_confirm', 'backup_full', 'backup_light'}
UNSAFE_KINDS = NETWORK_KINDS | {'upload', 'restore_preview', 'restore_confirm', 'backup_full', 'backup_light'}

def stage(uid, content, limit):
    from app.research.service import ResearchError
    if not content or len(content) > limit:
        raise ResearchError(413, 'invalid_file_size', '文件为空或超过大小限制')
    folder = store.data_dir() / 'task-staging' / hashlib.sha256(uid.encode()).hexdigest()
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    token = uuid.uuid4().hex
    path = folder / token
    with path.open('xb') as out:
        out.write(content)
    path.chmod(0o600)
    return {'stage_token': token, 'sha256': hashlib.sha256(content).hexdigest()}

def _stage_path(uid, token):
    import re
    if not re.fullmatch(r'[0-9a-f]{32}', token or ''):
        raise ValueError('文件暂存标识无效')
    return store.data_dir() / 'task-staging' / hashlib.sha256(uid.encode()).hexdigest() / token

def staged(uid, token, digest):
    path = _stage_path(uid, token)
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != digest:
        raise ValueError('暂存文件校验失败')
    return path, content

_backup_lock = threading.Lock()
_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='research-async')


def _public(row):
    result = store.model_dict(row)
    result.pop('user_id', None)
    result['request'] = store.json_value(result.pop('request_json'), {})
    result['result'] = store.json_value(result.pop('result_json'), {})
    return result


def get_task(uid, task_id):
    from app.research.service import ResearchError
    with migration_session() as conn:
        row = conn.scalar(select(store.ResearchAsyncTask).where(store.ResearchAsyncTask.id == task_id, store.ResearchAsyncTask.user_id == uid))
        if row is None:
            raise ResearchError(404, 'task_not_found', '任务不存在')
        return _public(row)


def list_tasks(uid):
    with migration_session() as conn:
        active = conn.scalars(select(store.ResearchAsyncTask).where(store.ResearchAsyncTask.user_id == uid, store.ResearchAsyncTask.status.in_(['queued', 'processing', 'cancelling', 'awaiting_approval'])).order_by(store.ResearchAsyncTask.created_at.desc())).all()
        recent = conn.scalars(select(store.ResearchAsyncTask).where(store.ResearchAsyncTask.user_id == uid, store.ResearchAsyncTask.status.not_in(['queued', 'processing', 'cancelling', 'awaiting_approval'])).order_by(store.ResearchAsyncTask.created_at.desc()).limit(100)).all()
        result = []
        for row in [*active, *recent]:
            item = _public(row)
            project = conn.scalar(select(store.ResearchProject).where(store.ResearchProject.id == row.project_id, store.ResearchProject.user_id == uid))
            item['project_name'] = '备份与恢复' if row.kind in GLOBAL_KINDS else (project.name if project else '课题已不存在')
            result.append(item)
        return result


def create(uid, project_id, kind, request):
    from app.research.service import ResearchError, _project, _document
    if kind not in {'chat', 'generation', 'upload'} | NETWORK_KINDS | GLOBAL_KINDS:
        raise ResearchError(400, 'task_kind_invalid', '不支持的任务类型')
    with memory_session() as conn:
        if kind not in GLOBAL_KINDS:
            _project(conn, uid, project_id)
        for other in request.get('cross_project_ids', []):
            _project(conn, uid, other)
        versions = {}
        for doc_id in request.get('document_ids', []):
            document = _document(conn, uid, doc_id)
            if document.project_id != project_id:
                raise ResearchError(400, 'document_project_mismatch', '资料不属于当前课题')
            versions[doc_id] = document.version
        request = {**request, '_versions': versions}
        task_id = 'tsk_' + uuid.uuid4().hex
        if (kind in NETWORK_KINDS or kind == 'chat') and not request.get('task_id'):
            request['task_id'] = task_id
        row = store.ResearchAsyncTask(id=task_id, user_id=uid, project_id=project_id, kind=kind,
            status='queued', request_json=json.dumps(request, ensure_ascii=False), result_json='{}', error='',
            created_at=store.utc_now(), started_at='', completed_at='')
        conn.add(row)
        conn.flush()
    _pool.submit(_run, task_id)
    return get_task(uid, task_id)


def _run(task_id):
    from app.data_access import system_scope
    from app.research.service import ResearchError, _document
    with system_scope(), task_scope(task_id):
        with memory_session() as conn:
            changed = conn.execute(update(store.ResearchAsyncTask).where(store.ResearchAsyncTask.id == task_id, store.ResearchAsyncTask.status == 'queued').values(status='processing', started_at=store.utc_now()))
            if changed.rowcount != 1:
                return
            row = conn.scalar(select(store.ResearchAsyncTask).where(store.ResearchAsyncTask.id == task_id))
            uid, project_id, kind = row.user_id, row.project_id, row.kind
            payload = json.loads(row.request_json)
            versions = payload.pop('_versions', {})
            stage_token = payload.get('stage_token')
        try:
            check_cancelled()
            with migration_session() as conn:
                for doc_id, version in versions.items():
                    if _document(conn, uid, doc_id).version != version:
                        raise ResearchError(409, 'document_version_changed', '资料版本已变化，请重新提交')
            if kind == 'chat':
                from app.research.service import chat
                result = chat(uid, project_id, **payload)
            elif kind == 'generation':
                from app.research.generations import generate_output
                result = generate_output(uid, project_id, **payload)
            elif kind == 'upload':
                from app.research.service import import_document
                path, content = staged(uid, payload.pop('stage_token'), payload.pop('sha256'))
                result = import_document(uid, project_id, content=content, **payload)
                path.unlink(missing_ok=True)
                if result.get('document_type') == 'paper' and result.get('processing_status') == 'ready':
                    try:
                        from app.research.batches import create_batch
                        create_batch(uid, project_id, [result['id']])
                    except Exception:
                        result['analysis_warning'] = '资料已导入，但未能自动安排分析，请手动分析。'
            elif kind in NETWORK_KINDS:
                from app.research import external_sources
                fn = {'arxiv_search': external_sources.search_arxiv, 'arxiv_import': external_sources.import_arxiv_paper, 'github_import': external_sources.import_github_repository, 'webpage_import': external_sources.import_webpage}[kind]
                result = fn(uid, project_id, **payload)
                document = result.get('document', {})
                if kind == 'arxiv_import' and document.get('processing_status') == 'ready':
                    try:
                        from app.research.batches import create_batch
                        create_batch(uid, project_id, [document['id']])
                    except Exception:
                        result['analysis_warning'] = '资料已导入，但未能自动安排分析，请手动分析。'
            elif kind == 'restore_preview':
                from app.research.restore_backup import preview
                path, content = staged(uid, payload['stage_token'], payload['sha256'])
                result = preview(uid, content)
                path.unlink(missing_ok=True)
            elif kind == 'restore_confirm':
                from app.research.restore_backup import restore
                result = restore(uid, **payload)
            else:
                from app.research import backup
                with _backup_lock:
                    result = (backup.export_library if kind == 'backup_full' else backup.export_lightweight)(uid, confirmed=True)
            with memory_session() as conn:
                row = conn.scalar(select(store.ResearchAsyncTask).where(store.ResearchAsyncTask.id == task_id))
                row.result_json = json.dumps(result, ensure_ascii=False)
                row.status = 'awaiting_approval' if result.get('status') == 'awaiting_outbound_approval' else 'completed'
                row.completed_at = '' if row.status == 'awaiting_approval' else store.utc_now()
        except Exception as exc:
            with memory_session() as conn:
                row = conn.scalar(select(store.ResearchAsyncTask).where(store.ResearchAsyncTask.id == task_id))
                row.status = 'cancelled' if isinstance(exc, TaskCancelled) else 'failed'
                row.error = '已停止，已完成的步骤可能保留' if isinstance(exc, TaskCancelled) else str(exc)[:1500]
                if kind in NETWORK_KINDS or payload.get('provider') == 'external':
                    approval = conn.scalar(select(store.ResearchOutboundApproval).where(
                        store.ResearchOutboundApproval.user_id == uid,
                        store.ResearchOutboundApproval.task_id == payload.get('task_id', ''),
                        store.ResearchOutboundApproval.status.in_(['sending', 'failed', 'sent', 'completed'])
                    ).order_by(store.ResearchOutboundApproval.created_at.desc()).limit(1))
                    if approval is not None:
                        row.status = 'unknown'
                        row.error = '外部请求已发出，最终结果未能安全保存；请核对服务记录，勿重复提交'
                if kind in {'upload', 'restore_confirm', 'backup_full', 'backup_light'} and not isinstance(exc, ResearchError):
                    row.status = 'unknown'
                    row.error = '处理可能已部分完成，请先核对资料库或备份文件，再决定是否重新发起'
                if isinstance(exc, ResearchError):
                    row.result_json = json.dumps({'code': exc.code, **exc.data}, ensure_ascii=False)
                row.completed_at = store.utc_now()
        finally:
            if stage_token:
                try:
                    _stage_path(uid, stage_token).unlink(missing_ok=True)
                except (OSError, ValueError):
                    pass


def cancel(uid, task_id):
    from app.research.service import ResearchError
    with migration_session() as conn:
        row = conn.scalar(select(store.ResearchAsyncTask).where(store.ResearchAsyncTask.id == task_id, store.ResearchAsyncTask.user_id == uid))
        if row is None:
            raise ResearchError(404, 'task_not_found', '任务不存在')
        status = row.status
        token = store.json_value(row.request_json, {}).get('stage_token')
        external = row.kind in UNSAFE_KINDS or store.json_value(row.request_json, {}).get('provider') == 'external'
    if status == 'processing':
        if external:
            raise ResearchError(409, 'external_outcome_uncertain', '外部调用可能已发出，无法确认取消；请等待结果，勿重复提交')
        if not request_cancel(task_id):
            raise ResearchError(409, 'task_not_cancellable', '任务状态正在变化，请刷新后查看')
    with memory_session() as conn:
        row = conn.scalar(select(store.ResearchAsyncTask).where(store.ResearchAsyncTask.id == task_id, store.ResearchAsyncTask.user_id == uid))
        if row.status == 'queued':
            pending_id = store.json_value(row.request_json, {}).get('approval_id')
            if pending_id:
                approval_row = conn.scalar(select(store.ResearchOutboundApproval).where(store.ResearchOutboundApproval.id == pending_id, store.ResearchOutboundApproval.user_id == uid))
                if approval_row and approval_row.status in ('pending', 'approved'):
                    approval_row.status = 'invalidated'
            row.status = 'cancelled'; row.completed_at = store.utc_now()
        elif row.status == 'processing' and not external:
            row.status = 'cancelling'
        elif row.status == 'awaiting_approval':
            approval = store.json_value(row.result_json, {}).get('approval', {})
            approval_row = conn.scalar(select(store.ResearchOutboundApproval).where(store.ResearchOutboundApproval.id == approval.get('id'), store.ResearchOutboundApproval.user_id == uid))
            if approval_row and approval_row.status in ('pending', 'approved'):
                approval_row.status = 'invalidated'
            row.status = 'cancelled'; row.completed_at = store.utc_now()
        elif row.status != 'cancelled':
            raise ResearchError(409, 'task_not_cancellable', '任务已结束或状态正在变化')
    if status == 'queued':
        if token:
            try:
                _stage_path(uid, token).unlink(missing_ok=True)
            except (OSError, ValueError):
                pass
    return get_task(uid, task_id)


def resume(uid, task_id):
    from app.research.service import ResearchError
    with memory_session() as conn:
        row = conn.scalar(select(store.ResearchAsyncTask).where(store.ResearchAsyncTask.id == task_id, store.ResearchAsyncTask.user_id == uid))
        if row is None:
            raise ResearchError(404, 'task_not_found', '任务不存在')
        result = store.json_value(row.result_json, {})
        approval = result.get('approval', {})
        approval_row = conn.scalar(select(store.ResearchOutboundApproval).where(store.ResearchOutboundApproval.id == approval.get('id'), store.ResearchOutboundApproval.user_id == uid))
        if row.status != 'awaiting_approval' or approval_row is None or approval_row.status != 'approved':
            raise ResearchError(409, 'approval_not_ready', '请先完成本次外发审批')
        payload = store.json_value(row.request_json, {})
        payload.update({'task_id': approval['task_id'], 'approval_id': approval['id']})
        if row.kind == 'chat':
            payload['conversation_id'] = result['conversation_id']
        row.request_json = json.dumps(payload, ensure_ascii=False)
        row.status = 'queued'; row.error = ''
    _pool.submit(_run, task_id)
    return get_task(uid, task_id)


def retry(uid, task_id):
    from app.research.service import ResearchError, _document
    with memory_session() as conn:
        row = conn.scalar(select(store.ResearchAsyncTask).where(store.ResearchAsyncTask.id == task_id, store.ResearchAsyncTask.user_id == uid))
        if row is None:
            raise ResearchError(404, 'task_not_found', '任务不存在')
        payload = store.json_value(row.request_json, {})
        if row.status != 'failed' or row.kind in UNSAFE_KINDS or payload.get('provider') == 'external':
            raise ResearchError(409, 'task_not_retryable', '该任务不能自动重试，请核对后重新发起')
        for doc_id, version in payload.get('_versions', {}).items():
            if _document(conn, uid, doc_id).version != version:
                raise ResearchError(409, 'document_version_changed', '资料版本已变化，请重新提交')
        row.status = 'queued'; row.error = ''; row.started_at = ''; row.completed_at = ''
    _pool.submit(_run, task_id)
    return get_task(uid, task_id)


def recover():
    from app.data_access import system_scope
    with system_scope(), memory_session() as conn:
        rows = conn.scalars(select(store.ResearchAsyncTask).where(store.ResearchAsyncTask.status.in_(['queued', 'processing', 'cancelling']))).all()
        for row in rows:
            external = row.kind in (NETWORK_KINDS | {'upload', 'restore_confirm', 'backup_full', 'backup_light'}) or store.json_value(row.request_json, {}).get('provider') == 'external'
            row.status = 'unknown' if external and row.status != 'queued' else 'failed'
            row.error = '服务中断；外部调用结果未知，请先核对，勿自动重试' if row.status == 'unknown' else '服务中断，请手动重新发起'
            row.completed_at = store.utc_now()
        return len(rows)
