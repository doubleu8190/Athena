"""用户可见会话消息 PostgreSQL ORM 映射。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from adapter.base import Base


def _json_type() -> JSON:
    """在 PostgreSQL 使用 JSONB，在 SQLite 测试中使用普通 JSON。"""
    return JSON().with_variant(JSONB(), "postgresql")


class ConversationMessageModel(Base):
    """用户可见会话消息表。"""

    __tablename__ = "conversation_messages"
    __table_args__ = (
        CheckConstraint(
            "role IN ('user', 'assistant')",
            name="ck_conversation_messages_role",
        ),
        CheckConstraint(
            "status IN ('pending', 'streaming', 'completed', 'failed', 'cancelled')",
            name="ck_conversation_messages_status",
        ),
        UniqueConstraint(
            "session_id",
            "sequence",
            name="uq_conversation_messages_session_sequence",
        ),
        UniqueConstraint(
            "session_id",
            "client_message_id",
            name="uq_conversation_messages_client",
        ),
        UniqueConstraint(
            "session_id",
            "run_id",
            "role",
            name="uq_conversation_messages_run_role",
        ),
        Index(
            "ix_conversation_messages_session_role_sequence",
            "session_id",
            "role",
            "sequence",
        ),
        Index(
            "ix_conversation_messages_session_created",
            "session_id",
            "created_at",
            "id",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    parts: Mapped[list[dict[str, object]]] = mapped_column(_json_type(), nullable=False)
    reply_to_message_id: Mapped[str | None] = mapped_column(
        ForeignKey("conversation_messages.id", ondelete="SET NULL"),
        nullable=True,
    )
    run_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    client_message_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    metadata_json: Mapped[dict[str, object]] = mapped_column(
        "metadata",
        _json_type(),
        nullable=False,
        default=dict,
    )


__all__ = ["ConversationMessageModel"]
