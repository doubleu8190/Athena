"""Session/Message 的 PostgreSQL ORM 模型。"""

from __future__ import annotations

from sqlalchemy import ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class SessionModel(Base):
    """sessions 表的基础设施映射。"""

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    title: Mapped[str] = mapped_column(String, default="New Session")
    status: Mapped[str] = mapped_column(String, default="idle")
    run_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String)
    updated_at: Mapped[str] = mapped_column(String)
    compression_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_compressed_message_id: Mapped[str | None] = mapped_column(
        String, nullable=True
    )
    last_summarized_message_id: Mapped[str | None] = mapped_column(
        String, nullable=True
    )
    deleted_time: Mapped[str | None] = mapped_column(String, nullable=True)


class MessageModel(Base):
    """messages 表的基础设施映射。"""

    __tablename__ = "messages"
    __table_args__ = (
        Index("idx_messages_session", "session_id"),
        Index("idx_messages_timestamp", "timestamp"),
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
