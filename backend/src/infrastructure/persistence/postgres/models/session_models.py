"""Session/Message 的 PostgreSQL ORM 模型。"""

from __future__ import annotations

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, Text
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
    creation_request_id: Mapped[str | None] = mapped_column(
        String, nullable=True, unique=True
    )
    title: Mapped[str] = mapped_column(String, default="新聊天")
    title_source: Mapped[str] = mapped_column(String, default="placeholder")
    title_revision: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String, default="idle")
    run_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String)
    updated_at: Mapped[str] = mapped_column(String)
    last_activity_at: Mapped[str] = mapped_column(String, nullable=False)
    compression_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_compressed_message_id: Mapped[str | None] = mapped_column(
        String, nullable=True
    )
    last_summarized_message_id: Mapped[str | None] = mapped_column(
        String, nullable=True
    )
    deleted_time: Mapped[str | None] = mapped_column(String, nullable=True)


class SessionTitleJobModel(Base):
    """持久化首条用户消息触发的会话标题生成任务。"""

    __tablename__ = "session_title_jobs"

    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), primary_key=True
    )
    source_message_id: Mapped[str] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), unique=True
    )
    expected_title_revision: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String, default="queued", index=True)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    claimed_by: Mapped[str | None] = mapped_column(String, nullable=True)
    available_at: Mapped[str] = mapped_column(String)
    lease_expires_at: Mapped[str | None] = mapped_column(String, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String, nullable=True)
    attachment_names_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[str] = mapped_column(String)
    updated_at: Mapped[str] = mapped_column(String)


class SessionMemorySettingsModel(Base):
    """会话级记忆读取与生成开关，独立于既有 sessions 表。"""

    __tablename__ = "session_memory_settings"

    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), primary_key=True
    )
    use_memories: Mapped[bool] = mapped_column(Boolean, default=True)
    generate_memories: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[str] = mapped_column(String)


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
    tool_calls_json: Mapped[str] = mapped_column(Text, default="[]")
    tool_call_id: Mapped[str | None] = mapped_column(String, nullable=True)
    run_id: Mapped[str | None] = mapped_column(String, nullable=True)
    tool_call_record_id: Mapped[str | None] = mapped_column(String, nullable=True)
    tool_name: Mapped[str | None] = mapped_column(String, nullable=True)
    message_type: Mapped[str | None] = mapped_column(String, nullable=True)
    timestamp: Mapped[str] = mapped_column(String)
    deleted_time: Mapped[str | None] = mapped_column(String, nullable=True)
