from __future__ import annotations

from sqlalchemy import Boolean, CheckConstraint, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class ChatAttachment(Base):
    __tablename__ = "chat_attachments"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(String, nullable=False)
    thread_id: Mapped[str] = mapped_column(String, default="")
    original_filename: Mapped[str] = mapped_column(String, nullable=False)
    stored_path: Mapped[str] = mapped_column(String, nullable=False)
    processed_path: Mapped[str] = mapped_column(String, default="")
    mime: Mapped[str] = mapped_column(String, nullable=False)
    media_kind: Mapped[str] = mapped_column(String, nullable=False)
    confidentiality: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    sha256: Mapped[str] = mapped_column(String, nullable=False)
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    source_text: Mapped[str] = mapped_column(Text, default="")
    chunks_json: Mapped[str] = mapped_column(Text, default="[]")
    processing_error: Mapped[str] = mapped_column(Text, default="")
    processing_mode: Mapped[str] = mapped_column(String, default="")
    embedding_model: Mapped[str] = mapped_column(String, default="")
    allow_outbound: Mapped[bool] = mapped_column(Boolean, default=False)
    provider: Mapped[str] = mapped_column(String, default="")
    model: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)
    expires_at: Mapped[str] = mapped_column(String, nullable=False)
    deleted_at: Mapped[str] = mapped_column(String, default="")
    __table_args__ = (
        CheckConstraint("confidentiality IN ('L1','L2','L3')"),
        CheckConstraint("media_kind IN ('image','document')"),
        Index("ix_chat_attachments_user_status", "user_id", "status"),
    )
