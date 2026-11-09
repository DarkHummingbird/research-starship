"""Preview and restore v2 backups into an empty account without overwriting files."""
from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import threading
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from app.data_access.research import memory_session, migration_session
from app.research import store

_LOCK = threading.RLock()
MAX_ARCHIVE = 200 * 1024 * 1024
MAX_EXPANDED = 1024 * 1024 * 1024
TABLES = {
    "projects": store.ResearchProject, "documents": store.ResearchDocument,
    "chunks": store.ResearchChunk, "embeddings": store.ResearchEmbedding,
    "memories": store.ResearchMemory, "conversations": store.ResearchConversation,
    "messages": store.ResearchMessage, "generations": store.ResearchGeneration,
    "document_versions": store.ResearchDocumentVersion, "changes": store.ResearchChange,
    "conflicts": store.ResearchConflict, "outbound_approvals": store.ResearchOutboundApproval,
    "batches": store.ResearchBatch, "batch_items": store.ResearchBatchItem,
    "security_events": store.ResearchSecurityEvent,
    "async_tasks": store.ResearchAsyncTask,
}


def _error(message: str):
    from app.research.service import ResearchError
    raise ResearchError(422, "backup_invalid", message)


def _stage_dir(user_id: str) -> Path:
    owner = hashlib.sha256(user_id.encode()).hexdigest()
    directory = store.data_dir() / "restore-staging" / owner
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    return directory


def _read_backup(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    from app.research.service import ResearchError
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            names = [item.filename for item in infos]
            if len(names) != len(set(names)) or len(names) > 50000:
                _error("备份包含重复或过多文件")
            if sum(item.file_size for item in infos) > MAX_EXPANDED:
                _error("备份展开后过大")
            if any(PurePosixPath(name).is_absolute() or '..' in PurePosixPath(name).parts for name in names):
                _error("备份包含不安全路径")
            if any(item.flag_bits & 1 for item in infos):
                _error("不支持加密的备份文件")
            if any(item.filename in ('manifest.json', 'research.json') and item.file_size > 50 * 1024 * 1024 for item in infos):
                _error("备份描述文件过大")
            manifest = json.loads(archive.read("manifest.json"))
            data = json.loads(archive.read("research.json"))
            if isinstance(data, dict):
                data.setdefault("async_tasks", [])
            if not isinstance(manifest, dict) or manifest.get("format") != "research-starship-backup/v2":
                _error("此备份格式不支持完整恢复；请使用新版完整备份")
            if not isinstance(data, dict) or any(not isinstance(data.get(key), list) for key in TABLES):
                _error("备份缺少完整数据或索引")
            if manifest.get("missing_originals"):
                _error("备份缺少原件或历史副本，不能作为完整备份恢复")
            owners = set()
            for key, model in TABLES.items():
                allowed = set(model.__table__.columns.keys())
                ids = []
                pk = "chunk_id" if key == "embeddings" else "id"
                for row in data[key]:
                    if key == 'batch_items' and isinstance(row, dict) and 'document_version' not in row:
                        row['document_version'] = 0  # Older v2 backups did not bind task versions.
                    if key == 'batch_items' and isinstance(row, dict) and 'operation' not in row:
                        row['operation'] = 'analysis'
                    if not isinstance(row, dict) or set(row) != allowed:
                        _error(f"备份数据字段不完整：{key}")
                    for column in model.__table__.columns:
                        value = row[column.name]
                        if value is None:
                            if not column.nullable:
                                _error(f"备份数据缺少必要值：{key}")
                            continue
                        expected_type = column.type.python_type
                        if not isinstance(value, expected_type) or (expected_type is int and isinstance(value, bool)):
                            _error(f"备份数据类型无效：{key}")
                    owners.add(row.get("user_id"))
                    ids.append(row.get(pk))
                if len(ids) != len(set(ids)) or any(value is None for value in ids):
                    _error(f"备份数据标识重复：{key}")
            if len(owners) > 1 or None in owners:
                _error("备份包含多个账号的数据")
            project_ids = {row['id'] for row in data['projects']}
            docs = {row['id']: row for row in data['documents']}
            doc_ids = set(docs)
            chunks = {row['id']: row for row in data['chunks']}
            for key, rows in data.items():
                if key not in TABLES: continue
                for row in rows:
                    if 'project_id' in row and row['project_id'] not in project_ids:
                        _error(f"备份包含不属于课题的数据：{key}")
            for row in data['documents']:
                if row['project_id'] not in project_ids:
                    _error("资料所属课题不存在")
            for row in data['chunks']:
                if row['document_id'] not in doc_ids or row['project_id'] != docs[row['document_id']]['project_id']:
                    _error("资料片段缺少所属资料")
                document = docs[row['document_id']]
                if row['document_version'] == document['version'] and row['confidentiality'] != document['confidentiality']:
                    _error("当前资料片段的保密等级不一致")
            for row in data['embeddings'] + data['memories']:
                chunk = chunks.get(row['chunk_id'])
                if not chunk or any(chunk[k] != row[k] for k in ('document_id', 'document_version', 'project_id')):
                    _error("记忆或检索索引与资料片段不一致")
                if 'active' in row and row['active']:
                    doc = docs[row['document_id']]
                    if doc['status'] != 'approved' or doc['approved_version'] != row['document_version'] or doc['version'] != row['document_version']:
                        _error("有效记忆与资料审核版本不一致")
                    if row['confidentiality'] != doc['confidentiality'] or chunk['confidentiality'] != doc['confidentiality']:
                        _error("有效记忆的保密等级不一致")
            for row in data['embeddings']:
                vector = json.loads(row['vector_json'])
                if (not isinstance(vector, list) or not isinstance(row['dimensions'], int)
                        or not 0 < row['dimensions'] <= 4096 or len(vector) != row['dimensions']
                        or any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) for value in vector)):
                    _error("备份中的检索索引无效")
            for row in data['document_versions']:
                if row['document_id'] not in doc_ids or row['project_id'] != docs[row['document_id']]['project_id']:
                    _error("历史版本缺少所属资料")
            for row in data['documents'] + data['document_versions']:
                member = row.get('stored_path')
                if not isinstance(member, str) or not (member.startswith('files/') or member.startswith('version-files/')) or member not in names:
                    _error("备份缺少资料副本")
                expected = row.get('sha256')
                if not isinstance(expected, str) or not re.fullmatch(r'[0-9a-f]{64}', expected):
                    _error("资料校验值无效")
                digest = hashlib.sha256()
                with archive.open(member) as source:
                    for block in iter(lambda: source.read(1024 * 1024), b''):
                        digest.update(block)
                if digest.hexdigest() != expected:
                    _error("资料副本校验失败")
            return manifest, data
    except ResearchError:
        raise
    except (zipfile.BadZipFile, KeyError, json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError, OverflowError, OSError) as exc:
        _error(f"备份无法读取或校验：{type(exc).__name__}")


def _occupied(conn, user_id):
    for model in TABLES.values():
        query = select(model.user_id).where(model.user_id == user_id)
        if model is store.ResearchAsyncTask:
            query = query.where(model.kind.not_in(['restore_preview', 'restore_confirm', 'backup_full', 'backup_light']))
        if conn.scalar(query.limit(1)) is not None:
            return True
    return False

def preview(user_id: str, content: bytes) -> dict[str, Any]:
    if not content or len(content) > MAX_ARCHIVE:
        _error("备份须为不超过 200 MB 的 ZIP 文件")
    token = uuid.uuid4().hex
    path = _stage_dir(user_id) / (token + '.zip')
    path.write_bytes(content)
    path.chmod(0o600)
    try:
        manifest, data = _read_backup(path)
        with migration_session() as conn:
            occupied = _occupied(conn, user_id)
        from app.data_access import system_scope
        conflict = False
        with system_scope(), migration_session() as conn:
            for key, model in TABLES.items():
                if key in ('messages', 'security_events'):
                    continue
                pk = 'chunk_id' if key == 'embeddings' else 'id'
                values = [row[pk] for row in data[key]]
                for start in range(0, len(values), 500):
                    if conn.scalar(select(getattr(model, pk)).where(getattr(model, pk).in_(values[start:start + 500])).limit(1)) is not None:
                        conflict = True
                        break
                if conflict:
                    break
        return {"token": token, "sha256": hashlib.sha256(content).hexdigest(),
                "project_count": len(data['projects']), "document_count": len(data['documents']),
                "memory_count": len(data['memories']), "version_count": len(data['document_versions']),
                "index_count": len(data['embeddings']), "created_at": manifest.get('created_at'),
                "can_restore": not occupied and not conflict,
                "notice": ("当前账号已有科研数据，不能恢复或覆盖。" if occupied else
                           "此数据目录已有相同资料标识，请使用全新的独立数据目录。" if conflict else
                           "仅可恢复到没有科研数据的账号；不会覆盖现有资料。未完成的外发审批和任务不会自动继续。")}
    except Exception:
        path.unlink(missing_ok=True)
        raise


def restore(user_id: str, token: str, expected_sha256: str, confirmed: bool) -> dict[str, Any]:
    from app.research.service import ResearchError
    if not confirmed:
        raise ResearchError(409, "restore_confirmation_required", "请先确认预览后再恢复")
    if not re.fullmatch(r'[0-9a-f]{32}', token or ''):
        _error("恢复预览标识无效")
    path = _stage_dir(user_id) / (token + '.zip')
    if not path.is_file():
        raise ResearchError(404, "restore_preview_missing", "预览已失效，请重新选择备份")
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected_sha256:
        raise ResearchError(409, "restore_preview_changed", "备份内容已变化，请重新预览")
    with _LOCK:
        manifest, data = _read_backup(path)
        destination = store.data_dir() / 'restored-files' / hashlib.sha256(user_id.encode()).hexdigest() / token
        if destination.exists():
            raise ResearchError(409, "restore_already_started", "这份预览已使用或恢复尚未清理")
        committed = False
        try:
            with memory_session() as conn:
                if _occupied(conn, user_id):
                    raise ResearchError(409, "restore_target_not_empty", "当前账号已有科研数据，不能覆盖或合并")
                destination.mkdir(parents=True, mode=0o700)
                with zipfile.ZipFile(path) as archive:
                    filenames = {item['id']: item['original_filename'] for item in data['documents']}
                    for key in ('documents', 'document_versions'):
                        for index, row in enumerate(data[key]):
                            original_name = row['original_filename'] if key == 'documents' else filenames[row['document_id']]
                            suffix = Path(original_name).suffix.lower()
                            if not re.fullmatch(r'\.[a-z0-9]{1,10}', suffix):
                                suffix = '.bin'
                            target = destination / f'{key}-{index}{suffix}'
                            digest = hashlib.sha256()
                            with archive.open(row['stored_path']) as source, target.open('xb') as output:
                                for block in iter(lambda: source.read(1024 * 1024), b''):
                                    digest.update(block)
                                    output.write(block)
                            if digest.hexdigest() != row['sha256']:
                                raise ResearchError(409, "restore_preview_changed", "资料副本在恢复时发生变化，请重新预览")
                            target.chmod(0o600)
                            row['stored_path'] = str(target)
                for key, model in TABLES.items():
                    for row in data[key]:
                        row = dict(row)
                        row['user_id'] = user_id
                        if key in ('messages', 'security_events'):
                            row.pop('id')
                        if key == 'outbound_approvals' and row['status'] in ('pending', 'approved', 'sending'):
                            row['status'] = 'invalidated'
                            row['error_code'] = 'restore_requires_new_approval'
                        if key == 'batch_items' and row['status'] in ('queued', 'processing', 'cancelling'):
                            row['status'] = 'failed'
                            row['error'] = '备份恢复后未自动继续，请手动重试'
                            row['completed_at'] = store.utc_now()
                        if key == 'batches' and row['status'] in ('queued', 'processing'):
                            row['status'] = 'completed_with_errors'
                            row['completed_at'] = store.utc_now()
                        if key == 'async_tasks' and row['status'] in ('queued', 'processing', 'cancelling', 'awaiting_approval'):
                            was_running = row['status'] in ('processing', 'cancelling')
                            external = row['kind'] in ('arxiv_search', 'arxiv_import', 'github_import', 'webpage_import', 'upload') or store.json_value(row['request_json'], {}).get('provider') == 'external'
                            row['status'] = 'unknown' if external and was_running else 'failed'
                            row['error'] = '备份恢复后任务未自动继续；外发状态可能未知，请核对后重新发起'
                            row['completed_at'] = store.utc_now()
                        conn.add(model(**row))
                    conn.flush()
            committed = True
            indexed = {row['chunk_id'] for row in data['embeddings']}
            pending: dict[str, list[str]] = {}
            for document in data['documents']:
                if document['status'] == 'deleted' or document['processing_status'] != 'ready':
                    continue
                current = [chunk['id'] for chunk in data['chunks'] if chunk['document_id'] == document['id'] and chunk['document_version'] == document['version']]
                if current and any(chunk_id not in indexed for chunk_id in current):
                    pending.setdefault(document['project_id'], []).append(document['id'])
            scheduled = 0
            scheduling_errors = 0
            from app.research.batches import create_batch
            for project_id, documents in pending.items():
                for start in range(0, len(documents), 5):
                    group = documents[start:start + 5]
                    try:
                        create_batch(user_id, project_id, group, operation='reindex')
                        scheduled += len(group)
                    except Exception:
                        scheduling_errors += len(group)
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            return {"project_count": len(data['projects']), "document_count": len(data['documents']),
                    "memory_count": len(data['memories']), "index_count": len(data['embeddings']),
                    "reindex_scheduled": scheduled, "reindex_scheduling_errors": scheduling_errors,
                    "message": f"恢复完成；已写回备份中的索引，{scheduled} 份资料的缺失索引已进入任务中心，{scheduling_errors} 份未能排队。未完成任务和外发审批不会自动继续。"}
        except IntegrityError as exc:
            if not committed: shutil.rmtree(destination, ignore_errors=True)
            raise ResearchError(409, "restore_id_conflict", "备份标识已存在，不能覆盖其他数据；请使用全新的独立数据目录") from exc
        except ResearchError:
            if not committed: shutil.rmtree(destination, ignore_errors=True)
            raise
        except (TypeError, ValueError, KeyError) as exc:
            if not committed: shutil.rmtree(destination, ignore_errors=True)
            raise ResearchError(422, "backup_invalid", "备份字段无效，恢复已回滚") from exc
        except Exception:
            if not committed: shutil.rmtree(destination, ignore_errors=True)
            raise
