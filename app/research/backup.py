from __future__ import annotations

import json
import zipfile
from pathlib import Path
from urllib.parse import quote
from typing import Any

from sqlalchemy import select

from app.data_access.research import migration_session
from app.research import store


def _rows(conn, model, user_id: str) -> list[dict[str, Any]]:
    rows = conn.scalars(select(model).where(model.user_id == user_id)).all()
    return [store.model_dict(row) for row in rows]


def export_library(user_id: str, *, confirmed: bool) -> dict[str, Any]:
    from app.research.service import ResearchError

    if not confirmed:
        raise ResearchError(409, "export_confirmation_required", "整库打包需要用户明确确认")
    with migration_session() as conn:
        projects = _rows(conn, store.ResearchProject, user_id)
        documents = _rows(conn, store.ResearchDocument, user_id)
        memories = _rows(conn, store.ResearchMemory, user_id)
        generations = _rows(conn, store.ResearchGeneration, user_id)
        versions = _rows(conn, store.ResearchDocumentVersion, user_id)
        changes = _rows(conn, store.ResearchChange, user_id)
        conflicts = _rows(conn, store.ResearchConflict, user_id)
        outbound = _rows(conn, store.ResearchOutboundApproval, user_id)
    manifest = {
        "format": "research-starship-backup/v1",
        "created_at": store.utc_now(),
        "project_count": len(projects),
        "document_count": len(documents),
        "memory_count": len(memories),
        "confidentiality_counts": {
            level: sum(document["confidentiality"] == level for document in documents)
            for level in ("L1", "L2", "L3")
        },
        "missing_originals": [],
        "includes": ["structured_cards", "available_system_copies", "versions", "changes", "conflicts"],
        "excludes": ["API keys", "external credentials", "remote export"],
    }
    directory = store.data_dir() / "exports" / user_id
    directory.mkdir(parents=True, exist_ok=True)
    stem = "research-starship-backup"
    target = directory / f"{stem}.zip"
    index = 2
    while target.exists():
        target = directory / f"{stem}-v{index}.zip"
        index += 1
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("research.json", json.dumps({
            "projects": projects, "documents": documents, "memories": memories,
            "generations": generations, "document_versions": versions,
            "changes": changes, "conflicts": conflicts, "outbound_approvals": outbound,
        }, ensure_ascii=False, indent=2))
        for document in documents:
            source = Path(document["stored_path"])
            if source.is_file():
                archive.write(source, f"files/{document['id']}/{document['original_filename']}")
            else:
                manifest["missing_originals"].append({
                    "document_id": document["id"], "title": document["title"], "reason": "系统副本不存在",
                })
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    return {
        "path": str(target), "filename": target.name, "size_bytes": target.stat().st_size,
        "document_count": len(documents), "missing_originals": manifest["missing_originals"],
        "remote_exported": False, "contains_levels": manifest["confidentiality_counts"],
    }


def export_lightweight(user_id: str, *, confirmed: bool) -> dict[str, Any]:
    from app.research.service import ResearchError

    if not confirmed:
        raise ResearchError(409, "export_confirmation_required", "轻量备份需要用户明确确认")
    with migration_session() as conn:
        projects = _rows(conn, store.ResearchProject, user_id)
        document_rows = _rows(conn, store.ResearchDocument, user_id)
        memory_rows = conn.scalars(select(store.ResearchMemory).where(
            store.ResearchMemory.user_id == user_id, store.ResearchMemory.active.is_(True),
        )).all()
        documents = [{
            key: row[key] for key in (
                "id", "project_id", "title", "original_filename", "sha256", "confidentiality",
                "version", "status", "document_type", "source_uri", "created_at", "updated_at", "approved_version",
            )
        } for row in document_rows if row["status"] != "deleted"]
        memories = [{
            key: value for key, value in store.model_dict(row).items() if key != "user_id"
        } for row in memory_rows]
    for project in projects:
        project.pop("user_id", None)
    payload = {
        "manifest": {
            "format": "research-starship-light-backup/v1", "created_at": store.utc_now(),
            "project_count": len(projects), "document_count": len(documents), "memory_count": len(memories),
            "includes": ["projects", "current_document_metadata", "active_memories"],
            "excludes": ["original_files", "historical_versions", "raw_model_output", "credentials", "outbound_approvals"],
            "full_restore_supported": False,
        },
        "projects": projects, "documents": documents, "memories": memories,
    }
    directory = store.data_dir() / "exports" / user_id
    directory.mkdir(parents=True, exist_ok=True)
    stem = "research-starship-light-backup"
    target = directory / f"{stem}.json"
    index = 2
    while target.exists():
        target = directory / f"{stem}-v{index}.json"
        index += 1
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    target.chmod(0o600)
    return {
        "path": str(target), "filename": target.name, "size_bytes": target.stat().st_size,
        "document_count": len(documents), "memory_count": len(memories),
        "download_url": f"/api/research/export/light/download?filename={quote(target.name)}",
        "includes_originals": False, "includes_history": False, "full_restore_supported": False,
        "remote_exported": False,
    }
