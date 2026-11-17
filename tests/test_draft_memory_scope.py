from __future__ import annotations

from app.agent.context_assembler import memory_context
from app.agent.memory import write_event
from app.data_access import user_scope


def test_draft_memory_excludes_previous_chat_content():
    uid = "draft-memory-scope-user"
    with user_scope(uid):
        write_event(uid, "chat", "上一轮请假对话的助手回答原文", thread_id="t1")
        full = memory_context(uid, query="帮我写一封请假邮件", limit=6)
        scoped = memory_context(uid, query="帮我写一封请假邮件", limit=6, include_episodes=False)
    assert "上一轮请假对话的助手回答原文" in full
    assert "上一轮请假对话的助手回答原文" not in scoped
    assert "相关记忆" not in scoped


def test_draft_prompt_forbids_copying_memory_into_body():
    from app.tools.writing import DRAFT_SYSTEM

    assert "不得复制或改写进正文" in DRAFT_SYSTEM