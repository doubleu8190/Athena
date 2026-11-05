"""模型执行轨迹消息 PostgreSQL ORM 映射。"""

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


class ExecutionMessageModel(Base):
    """模型实际执行轨迹消息表。"""

    __tablename__ = "execution_messages"
    __table_args__ = (
        CheckConstraint(
            "role IN ('system', 'user', 'assistant', 'tool')",
            name="ck_execution_messages_role",
        ),
        CheckConstraint(
            "(role = 'tool' AND tool_call_id IS NOT NULL) "
            "OR (role <> 'tool' AND tool_call_id IS NULL)",
            name="ck_execution_messages_tool_call",
        ),
        UniqueConstraint(
            "session_id",
            "sequence",
            name="uq_execution_messages_session_sequence",
        ),
        UniqueConstraint(
            "session_id",
            "run_id",
            "thread_id",
            "tool_call_id",
            name="uq_execution_messages_scope_tool",
        ),
        Index(
            "ix_execution_messages_thread_sequence",
            "thread_id",
            "sequence",
        ),
        Index(
            "ix_execution_messages_run_sequence",
            "run_id",
            "sequence",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    run_id: Mapped[str] = mapped_column(String(128), nullable=False)
    thread_id: Mapped[str] = mapped_column(String(128), nullable=False)
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    parts: Mapped[list[dict[str, object]]] = mapped_column(_json_type(), nullable=False)
    tool_call_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source_message_id: Mapped[str | None] = mapped_column(
        ForeignKey("conversation_messages.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    metadata_json: Mapped[dict[str, object]] = mapped_column(
        "metadata",
        _json_type(),
        nullable=False,
        default=dict,
    )


__all__ = ["ExecutionMessageModel"]
