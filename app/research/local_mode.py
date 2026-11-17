from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.data_access.research import memory_session, migration_session
from app.research import store
from app.research.service import ResearchError


MESSAGE = "本地保密模式默认关闭；请先在“数据与隐私 → 模型与隐私中心”中开启并加载本地模型"


def is_enabled(user_id: str) -> bool:
    with migration_session() as conn:
        row = conn.execute(select(store.ResearchUserSetting).where(
            store.ResearchUserSetting.user_id == user_id,
        )).scalar_one_or_none()
        return bool(row and row.local_mode_enabled)


def status(user_id: str) -> dict[str, Any]:
    from app.research.local_components import component_status

    enabled = is_enabled(user_id)
    components = component_status()
    model_ready = bool(components["generative_llm"]["available"])
    return {
        "enabled": enabled,
        "l3_available": enabled and model_ready,
        "local_generation_available": enabled and model_ready,
        "semantic_chunking_available": enabled and bool(components["embedding"]["available"]),
        "message": MESSAGE,
    }


def set_enabled(user_id: str, enabled: bool) -> dict[str, Any]:
    with memory_session() as conn:
        row = conn.execute(select(store.ResearchUserSetting).where(
            store.ResearchUserSetting.user_id == user_id,
        )).scalar_one_or_none()
        if row is None:
            row = store.ResearchUserSetting(
                user_id=user_id, local_mode_enabled=enabled, updated_at=store.utc_now(),
            )
            conn.add(row)
        else:
            row.local_mode_enabled = enabled
            row.updated_at = store.utc_now()
        conn.flush()
    return status(user_id)


def require_enabled(user_id: str) -> None:
    if not is_enabled(user_id):
        raise ResearchError(403, "local_mode_disabled", MESSAGE)


def require_l3(user_id: str) -> None:
    require_enabled(user_id)
