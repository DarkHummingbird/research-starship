from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import Boolean, CheckConstraint, Index, Integer, String, Text, UniqueConstraint, create_engine, select, text, update
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.db import Base as ResearchBase
from app.data_access.session import make_session_factory


class ResearchUserSetting(ResearchBase):
    __tablename__ = "research_user_settings"
    user_id: Mapped[str] = mapped_column(String, primary_key=True)
    local_mode_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)


class ResearchProject(ResearchBase):
    __tablename__ = "research_projects"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    goal: Mapped[str] = mapped_column(Text, default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)
    current_baseline_id: Mapped[str] = mapped_column(String, default="")
    research_question: Mapped[str] = mapped_column(Text, default="")
    next_action: Mapped[str] = mapped_column(Text, default="")


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
    document_type: Mapped[str] = mapped_column(String, default="tutorial")
    processing_status: Mapped[str] = mapped_column(String, default="ready")
    embedding_status: Mapped[str] = mapped_column(String, default="not_started")
    analysis_status: Mapped[str] = mapped_column(String, default="not_required")
    analysis_json: Mapped[str] = mapped_column(Text, default="{}")
    analysis_raw_json: Mapped[str] = mapped_column(Text, default="")
    processing_error: Mapped[str] = mapped_column(Text, default="")
    source_uri: Mapped[str] = mapped_column(String, default="")
    extraction_model: Mapped[str] = mapped_column(String, default="")
    deleted_at: Mapped[str] = mapped_column(String, default="")
    delete_after: Mapped[str] = mapped_column(String, default="")
    permanent_deleted_at: Mapped[str] = mapped_column(String, default="")
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
    locator_json: Mapped[str] = mapped_column(Text, default="{}")
    __table_args__ = (
        UniqueConstraint("document_id", "document_version", "ordinal"),
        CheckConstraint("confidentiality IN ('L1','L2','L3')"),
    )


class ResearchEmbedding(ResearchBase):
    __tablename__ = "research_embeddings"
    chunk_id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(String, nullable=False)
    project_id: Mapped[str] = mapped_column(String, nullable=False)
    document_id: Mapped[str] = mapped_column(String, nullable=False)
    document_version: Mapped[int] = mapped_column(Integer, nullable=False)
    vector_json: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(String, nullable=False)
    dimensions: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)


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
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    fact_json: Mapped[str] = mapped_column(Text, default="{}")
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


class ResearchGeneration(ResearchBase):
    __tablename__ = "research_generations"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(String, nullable=False)
    project_id: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    sources_json: Mapped[str] = mapped_column(Text, default="[]")
    model: Mapped[str] = mapped_column(String, nullable=False)
    confidentiality: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, default="pending")
    archived_document_id: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[str] = mapped_column(String, nullable=False)


class ResearchConflict(ResearchBase):
    __tablename__ = "research_conflicts"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(String, nullable=False)
    project_id: Mapped[str] = mapped_column(String, nullable=False)
    candidate_document_id: Mapped[str] = mapped_column(String, nullable=False)
    existing_memory_id: Mapped[str] = mapped_column(String, nullable=False)
    existing_fact_json: Mapped[str] = mapped_column(Text, nullable=False)
    candidate_fact_json: Mapped[str] = mapped_column(Text, nullable=False)
    comparability: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, default="pending")
    resolution: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    resolved_at: Mapped[str] = mapped_column(String, default="")


class ResearchDocumentVersion(ResearchBase):
    __tablename__ = "research_document_versions"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(String, nullable=False)
    project_id: Mapped[str] = mapped_column(String, nullable=False)
    document_id: Mapped[str] = mapped_column(String, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    confidentiality: Mapped[str] = mapped_column(String, nullable=False)
    parsed_json: Mapped[str] = mapped_column(Text, nullable=False)
    analysis_json: Mapped[str] = mapped_column(Text, nullable=False)
    analysis_raw_json: Mapped[str] = mapped_column(Text, default="")
    source_text: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(String, nullable=False)
    stored_path: Mapped[str] = mapped_column(String, nullable=False)
    document_type: Mapped[str] = mapped_column(String, nullable=False)
    processing_status: Mapped[str] = mapped_column(String, nullable=False)
    analysis_status: Mapped[str] = mapped_column(String, nullable=False)
    extraction_model: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    operation: Mapped[str] = mapped_column(String, nullable=False)


class ResearchChange(ResearchBase):
    __tablename__ = "research_changes"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(String, nullable=False)
    project_id: Mapped[str] = mapped_column(String, nullable=False)
    object_type: Mapped[str] = mapped_column(String, nullable=False)
    object_id: Mapped[str] = mapped_column(String, nullable=False)
    operation: Mapped[str] = mapped_column(String, nullable=False)
    document_version: Mapped[int] = mapped_column(Integer, nullable=False)
    before_json: Mapped[str] = mapped_column(Text, nullable=False)
    after_json: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    undone_at: Mapped[str] = mapped_column(String, default="")


class ResearchBatch(ResearchBase):
    __tablename__ = "research_batches"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(String, nullable=False)
    project_id: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    item_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    completed_at: Mapped[str] = mapped_column(String, default="")


class ResearchBatchItem(ResearchBase):
    __tablename__ = "research_batch_items"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    batch_id: Mapped[str] = mapped_column(String, nullable=False)
    user_id: Mapped[str] = mapped_column(String, nullable=False)
    project_id: Mapped[str] = mapped_column(String, nullable=False)
    document_id: Mapped[str] = mapped_column(String, nullable=False)
    document_version: Mapped[int] = mapped_column(Integer, default=0)
    operation: Mapped[str] = mapped_column(String, default="analysis")
    approval_id: Mapped[str] = mapped_column(String, default="")
    status: Mapped[str] = mapped_column(String, nullable=False)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    started_at: Mapped[str] = mapped_column(String, default="")
    completed_at: Mapped[str] = mapped_column(String, default="")


class ResearchAsyncTask(ResearchBase):
    __tablename__ = "research_async_tasks"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(String, nullable=False)
    project_id: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    request_json: Mapped[str] = mapped_column(Text, nullable=False)
    result_json: Mapped[str] = mapped_column(Text, default="{}")
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    started_at: Mapped[str] = mapped_column(String, default="")
    completed_at: Mapped[str] = mapped_column(String, default="")


class ResearchStateEntry(ResearchBase):
    __tablename__ = "research_state_entries"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(String, nullable=False)
    project_id: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    definition: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String, nullable=False)
    source_refs_json: Mapped[str] = mapped_column(Text, default="[]")
    related_json: Mapped[str] = mapped_column(Text, default="{}")
    abbreviation: Mapped[str] = mapped_column(String, default="")
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)
    __table_args__ = (
        CheckConstraint("kind IN ('open_question','evidence_gap','todo','term','review_notice')"),
        CheckConstraint("status IN ('active','resolved','pending_review','archived')"),
        Index("idx_research_state_project", "user_id", "project_id", "status"),
    )


class ResearchExperiment(ResearchBase):
    __tablename__ = "research_experiments"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(String, nullable=False)
    project_id: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    hypothesis: Mapped[str] = mapped_column(Text, default="")
    variables: Mapped[str] = mapped_column(Text, default="")
    controls: Mapped[str] = mapped_column(Text, default="")
    data_version: Mapped[str] = mapped_column(String, default="")
    code_version: Mapped[str] = mapped_column(String, default="")
    status: Mapped[str] = mapped_column(String, nullable=False)
    result: Mapped[str] = mapped_column(Text, default="")
    result_source_refs_json: Mapped[str] = mapped_column(Text, default="[]")
    failure_reason: Mapped[str] = mapped_column(Text, default="")
    next_step: Mapped[str] = mapped_column(Text, default="")
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)
    completed_at: Mapped[str] = mapped_column(String, default="")
    status_before_review: Mapped[str] = mapped_column(String, default="")
    review_reason: Mapped[str] = mapped_column(Text, default="")
    __table_args__ = (
        CheckConstraint("status IN ('idea','in_progress','succeeded','failed','pending_review')"),
        Index("idx_research_experiments_project", "user_id", "project_id", "status"),
    )


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

def note_source_document(conn, user_id: str, document_id: str):
    return conn.execute(select(ResearchDocument).where(
        ResearchDocument.id == document_id,
        ResearchDocument.user_id == user_id,
    )).scalar_one_or_none()


def note_source_document_by_id(conn, document_id: str):
    return conn.get(ResearchDocument, document_id)


def note_source_chunks(conn, document_id: str, version: int):
    return conn.execute(select(ResearchChunk).where(
        ResearchChunk.document_id == document_id,
        ResearchChunk.document_version == version,
    ).order_by(ResearchChunk.ordinal)).scalars().all()


def note_source_chunk(conn, chunk_id: str, document_id: str, version: int):
    return conn.execute(select(ResearchChunk).where(
        ResearchChunk.id == chunk_id,
        ResearchChunk.document_id == document_id,
        ResearchChunk.document_version == version,
    )).scalar_one_or_none()


def notes_dependent_on_source(conn, source):
    notes = conn.execute(select(ResearchDocument).where(
        ResearchDocument.user_id == source.user_id,
        ResearchDocument.project_id == source.project_id,
        ResearchDocument.document_type.in_(("research_note", "baseline")),
        ResearchDocument.status != "deleted",
    )).scalars().all()
    return [note for note in notes if any(
        ref.get("document_id") == source.id
        for ref in (json.loads(note.parsed_json or "{}").get("note_provenance") or [])
    )]


def deactivate_note_memories(conn, note_id: str) -> None:
    conn.execute(update(ResearchMemory).where(
        ResearchMemory.document_id == note_id
    ).values(active=False))


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
        additions = {
            "research_projects": {"current_baseline_id": "VARCHAR DEFAULT ''", "research_question": "TEXT DEFAULT ''", "next_action": "TEXT DEFAULT ''"},
            "research_documents": {
                "document_type": "VARCHAR DEFAULT 'tutorial'",
                "processing_status": "VARCHAR DEFAULT 'ready'",
                "embedding_status": "VARCHAR DEFAULT 'not_started'",
                "analysis_status": "VARCHAR DEFAULT 'not_required'",
                "analysis_json": "TEXT DEFAULT '{}'",
                "analysis_raw_json": "TEXT DEFAULT ''",
                "processing_error": "TEXT DEFAULT ''",
                "source_uri": "VARCHAR DEFAULT ''",
                "extraction_model": "VARCHAR DEFAULT ''",
                "deleted_at": "VARCHAR DEFAULT ''",
                "delete_after": "VARCHAR DEFAULT ''",
                "permanent_deleted_at": "VARCHAR DEFAULT ''",
            },
            "research_memories": {
                "active": "BOOLEAN DEFAULT 1",
                "fact_json": "TEXT DEFAULT '{}'",
            },
            "research_chunks": {"locator_json": "TEXT DEFAULT '{}'"},
            "research_batch_items": {"document_version": "INTEGER DEFAULT 0", "operation": "VARCHAR DEFAULT 'analysis'", "approval_id": "VARCHAR DEFAULT ''"},
            "research_state_entries": {"abbreviation": "VARCHAR DEFAULT ''"},
            "research_experiments": {"status_before_review": "VARCHAR DEFAULT ''", "review_reason": "TEXT DEFAULT ''"},
        }
        with engine.begin() as connection:
            for table_name, columns in additions.items():
                existing = {row[1] for row in connection.execute(text(f"PRAGMA table_info({table_name})"))}
                for name, ddl in columns.items():
                    if name not in existing:
                        connection.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {name} {ddl}"))
        _schema_paths.add(path)


@contextmanager
def memory_session(existing: Session | None = None, *, commit_on: tuple[type[BaseException], ...] = ()) -> Iterator[Session]:
    if existing is not None:
        yield existing
        return
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
