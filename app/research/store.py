from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.data_access.research import (
    ResearchBatch,
    ResearchBatchItem,
    ResearchAsyncTask,
    ResearchChange,
    ResearchChunk,
    ResearchConflict,
    ResearchConversation,
    ResearchDocument,
    ResearchDocumentVersion,
    ResearchEmbedding,
    ResearchExperiment,
    ResearchStateEntry,
    ResearchUserSetting,
    ResearchGeneration,
    ResearchMemory,
    ResearchMessage,
    ResearchOutboundApproval,
    ResearchProject,
    ResearchSecurityEvent,
    data_dir,
    db_path,
    init_db,
    model_dict,
    utc_now,
)


__all__ = [
    "ResearchBatch", "ResearchBatchItem", "ResearchAsyncTask", "ResearchChange", "ResearchChunk", "ResearchConflict",
    "ResearchConversation", "ResearchDocument", "ResearchDocumentVersion", "ResearchEmbedding", "ResearchExperiment", "ResearchStateEntry", "ResearchUserSetting",
    "ResearchGeneration", "ResearchMemory", "ResearchMessage", "ResearchOutboundApproval",
    "ResearchProject", "ResearchSecurityEvent", "data_dir", "db_path", "init_db",
    "model_dict", "utc_now", "json_value", "public_document", "public_approval",
]


def json_value(value: str, default: Any) -> Any:
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _dict(row: Any) -> dict[str, Any]:
    return model_dict(row) if hasattr(row, "__table__") else dict(row)


def public_document(row: Any, *, include_text: bool = False) -> dict[str, Any]:
    item = _dict(row)
    item["parsed"] = json_value(item.pop("parsed_json", "{}"), {})
    item["analysis"] = json_value(item.pop("analysis_json", "{}"), {})
    raw_model_output = item.pop("analysis_raw_json", "")
    item.pop("user_id", None)
    stored_path = item.pop("stored_path", "")
    source_text = item.pop("source_text", "")
    item["system_copy"] = Path(stored_path).name
    item["original_preserved"] = True
    if include_text:
        item["source_text"] = source_text
        item["raw_model_output"] = raw_model_output or None
    return item


def public_approval(row: Any) -> dict[str, Any]:
    item = _dict(row)
    item["scope"] = json_value(item.pop("scope_json", "{}"), {})
    item.pop("user_id", None)
    item.pop("scope_hash", None)
    return item
