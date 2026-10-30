"""Session/Message 的 PostgreSQL ORM 模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class SessionModel(Base):
    """sessions 表的基础设施映射。"""

    __tablename__ = "sessions"
    __table_args__ = (
        Index(
            "idx_sessions_project_activity",
            "project_id",
            "deleted_time",
            "last_activity_at",
            "id",
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String, default="新聊天")
    active_run_id: Mapped[str | None] = mapped_column(String, nullable=True)
    last_run_sequence: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_activity_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    compression_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_compressed_message_id: Mapped[str | None] = mapped_column(
        String, nullable=True
    )
    last_summarized_message_id: Mapped[str | None] = mapped_column(
        String, nullable=True
    )
    deleted_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SessionMemorySettingsModel(Base):
    """会话级记忆读取设置，独立于既有 sessions 表。"""

    __tablename__ = "session_memory_settings"

    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), primary_key=True
    )
    use_memories: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class MessageModel(Base):
    """messages 表的基础设施映射。"""

    __tablename__ = "messages"
    __table_args__ = (
        Index("idx_messages_session", "session_id"),
        Index("idx_messages_timestamp", "timestamp"),
        Index("idx_messages_session_timestamp_id", "session_id", "timestamp", "id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"))
    role: Mapped[str] = mapped_column(String)
    content: Mapped[str] = mapped_column(Text, default="")
    tool_calls_json: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    tool_call_id: Mapped[str | None] = mapped_column(String, nullable=True)
    run_id: Mapped[str | None] = mapped_column(String, nullable=True)
    tool_call_record_id: Mapped[str | None] = mapped_column(String, nullable=True)
    tool_name: Mapped[str | None] = mapped_column(String, nullable=True)
    message_type: Mapped[str | None] = mapped_column(String, nullable=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    deleted_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
