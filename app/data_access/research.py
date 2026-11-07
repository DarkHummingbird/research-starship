from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import CheckConstraint, Index, Integer, String, Text, UniqueConstraint, create_engine, text
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.db import Base as ResearchBase
from app.data_access.session import make_session_factory


class ResearchProject(ResearchBase):
    __tablename__ = "research_projects"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    goal: Mapped[str] = mapped_column(Text, default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)


class ResearchDocument(ResearchBase):
    __tablename__ = "research_documents"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(String, nullable=False)
    project_id: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    original_filename: Mapped[str] = mapped_column(String, nullable=False)
    stored_path: Mapped[str] = mapped_column(String, nullable=False)
    source_text: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(String, nullable=False)
    confidentiality: Mapped[str] = mapped_column(String, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String, nullable=False)
    parsed_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)
    approved_version: Mapped[int | None] = mapped_column(Integer)
    __table_args__ = (CheckConstraint("confidentiality IN ('L1','L2','L3')"),)


class ResearchChunk(ResearchBase):
    __tablename__ = "research_chunks"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(String, nullable=False)
    project_id: Mapped[str] = mapped_column(String, nullable=False)
    document_id: Mapped[str] = mapped_column(String, nullable=False)
    document_version: Mapped[int] = mapped_column(Integer, nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    chapter: Mapped[str] = mapped_column(String, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    confidentiality: Mapped[str] = mapped_column(String, nullable=False)
    __table_args__ = (
        UniqueConstraint("document_id", "document_version", "ordinal"),
        CheckConstraint("confidentiality IN ('L1','L2','L3')"),
    )


class ResearchMemory(ResearchBase):
    __tablename__ = "research_memories"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(String, nullable=False)
    project_id: Mapped[str] = mapped_column(String, nullable=False)
    document_id: Mapped[str] = mapped_column(String, nullable=False)
    document_version: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_id: Mapped[str] = mapped_column(String, nullable=False)
    entry_id: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    chapter: Mapped[str] = mapped_column(String, nullable=False)
    record_type: Mapped[str] = mapped_column(String, nullable=False)
    confidentiality: Mapped[str] = mapped_column(String, nullable=False)
    review_status: Mapped[str] = mapped_column(String, default="confirmed")
    confirmed_at: Mapped[str] = mapped_column(String, nullable=False)
    __table_args__ = (
        UniqueConstraint("document_id", "document_version", "entry_id"),
        CheckConstraint("confidentiality IN ('L1','L2','L3')"),
    )


class ResearchConversation(ResearchBase):
    __tablename__ = "research_conversations"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(String, nullable=False)
    project_id: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)


class ResearchMessage(ResearchBase):
    __tablename__ = "research_messages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[str] = mapped_column(String, nullable=False)
    user_id: Mapped[str] = mapped_column(String, nullable=False)
    project_id: Mapped[str] = mapped_column(String, nullable=False)
    role: Mapped[str] = mapped_column(String, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    confidentiality: Mapped[str] = mapped_column(String, nullable=False)
    sources_json: Mapped[str] = mapped_column(Text, default="[]")
    model: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)


class ResearchOutboundApproval(ResearchBase):
    __tablename__ = "research_outbound_approvals"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(String, nullable=False)
    project_id: Mapped[str] = mapped_column(String, nullable=False)
    task_id: Mapped[str] = mapped_column(String, nullable=False)
    provider: Mapped[str] = mapped_column(String, nullable=False)
    model_id: Mapped[str] = mapped_column(String, nullable=False)
    destination: Mapped[str] = mapped_column(String, nullable=False)
    purpose: Mapped[str] = mapped_column(String, nullable=False)
    operation: Mapped[str] = mapped_column(String, nullable=False)
    confidentiality: Mapped[str] = mapped_column(String, nullable=False)
    scope_json: Mapped[str] = mapped_column(Text, nullable=False)
    scope_hash: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    conversation_id: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    decided_at: Mapped[str | None] = mapped_column(String)
    completed_at: Mapped[str | None] = mapped_column(String)
    error_code: Mapped[str] = mapped_column(String, default="")


class ResearchSecurityEvent(ResearchBase):
    __tablename__ = "research_security_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String, nullable=False)
    project_id: Mapped[str] = mapped_column(String, nullable=False)
    conversation_id: Mapped[str] = mapped_column(String, default="")
    approval_id: Mapped[str] = mapped_column(String, default="")
    classification: Mapped[str] = mapped_column(String, nullable=False)
    operation: Mapped[str] = mapped_column(String, nullable=False)
    provider: Mapped[str] = mapped_column(String, nullable=False)
    purpose: Mapped[str] = mapped_column(String, nullable=False)
    outcome: Mapped[str] = mapped_column(String, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String, nullable=False)
    snippet: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)


Index("idx_research_projects_user", ResearchProject.user_id)
Index("idx_research_documents_project", ResearchDocument.user_id, ResearchDocument.project_id)
Index("idx_research_chunks_document", ResearchChunk.document_id, ResearchChunk.document_version)
Index("idx_research_memories_project", ResearchMemory.user_id, ResearchMemory.project_id)
Index("idx_research_messages_conversation", ResearchMessage.conversation_id, ResearchMessage.id)
Index("idx_research_outbound_task", ResearchOutboundApproval.user_id, ResearchOutboundApproval.task_id)
Index("idx_research_security_user", ResearchSecurityEvent.user_id, ResearchSecurityEvent.project_id, ResearchSecurityEvent.id)

_engines: dict[Path, object] = {}
_factories: dict[Path, object] = {}
_schema_paths: set[Path] = set()
_lock = threading.RLock()


def utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def data_dir() -> Path:
    configured = (os.getenv("RESEARCH_DATA_DIR") or "").strip()
    root = Path(configured).expanduser() if configured else Path(os.getenv("DATA_DIR", "./data")) / "research"
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def db_path() -> Path:
    return data_dir() / "research.db"


def _factory():
    path = db_path()
    with _lock:
        if path not in _factories:
            engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
            _engines[path] = engine
            _factories[path] = make_session_factory(engine)
        return _factories[path]


def SessionLocal() -> Session:
    return _factory()()


def init_db() -> None:
    path = db_path()
    with _lock:
        if path in _schema_paths:
            return
        engine = _engines.get(path)
        if engine is None:
            _factory()
            engine = _engines[path]
        tables = [table for name, table in ResearchBase.metadata.tables.items() if name.startswith("research_")]
        ResearchBase.metadata.create_all(engine, tables=tables)
        _schema_paths.add(path)


@contextmanager
def memory_session(*, commit_on: tuple[type[BaseException], ...] = ()) -> Iterator[Session]:
    init_db()
    with _lock:
        session = SessionLocal()
        try:
            connection = session.connection()
            if connection.dialect.name == "sqlite":
                session.execute(text("BEGIN IMMEDIATE"))
            yield session
            session.commit()
        except Exception as exc:
            if isinstance(exc, commit_on):
                session.commit()
            else:
                session.rollback()
            raise
        finally:
            session.close()


@contextmanager
def migration_session() -> Iterator[Session]:
    init_db()
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def model_dict(row) -> dict:
    return {column.name: getattr(row, column.name) for column in row.__table__.columns}
