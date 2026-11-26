"""Run tool-call lifecycle ORM mapping."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from adapter.base import Base


class RunToolCallModel(Base):
    """One admitted model-issued tool call and its unique protocol result."""

    __tablename__ = "run_tool_calls"
    __table_args__ = (
        CheckConstraint("ordinal > 0", name="ck_run_tool_calls_ordinal"),
        CheckConstraint("execution_epoch > 0", name="ck_run_tool_calls_epoch"),
        CheckConstraint(
            "status IN ('pending_approval', 'approved', 'executing', "
            "'completed', 'failed', 'cancelled', 'unknown')",
            name="ck_run_tool_calls_status",
        ),
        CheckConstraint(
            "tool_kind IS NULL OR tool_kind IN ('builtin', 'mcp')",
            name="ck_run_tool_calls_kind",
        ),
        UniqueConstraint("run_id", "tool_call_id", name="uq_run_tool_calls_protocol_id"),
        UniqueConstraint("run_id", "ordinal", name="uq_run_tool_calls_ordinal"),
        UniqueConstraint("result_message_id", name="uq_run_tool_calls_result_message"),
        ForeignKeyConstraint(
            ["run_id", "session_id"],
            ["runs.id", "runs.session_id"],
            name="fk_run_tool_calls_run_session",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["call_message_id", "session_id"],
            ["execution_messages.id", "execution_messages.session_id"],
            name="fk_run_tool_calls_call_message_session",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["result_message_id", "session_id"],
            ["execution_messages.id", "execution_messages.session_id"],
            name="fk_run_tool_calls_result_message_session",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "approval_decision IS NULL OR approval_decision IN ('approve', 'reject')",
            name="ck_run_tool_calls_approval_decision",
        ),
        Index("ix_run_tool_calls_call_message", "call_message_id"),
        Index("ix_run_tool_calls_run_status", "run_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(128), nullable=False)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    tool_call_id: Mapped[str] = mapped_column(String(256), nullable=False)
    ordinal: Mapped[int] = mapped_column(BigInteger, nullable=False)
    tool_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tool_kind: Mapped[str | None] = mapped_column(String(16), nullable=True)
    model_name: Mapped[str] = mapped_column(String(256), nullable=False)
    execution_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    call_message_id: Mapped[str] = mapped_column(String(64), nullable=False)
    result_message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    requires_approval: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    approval_decision: Mapped[str | None] = mapped_column(String(8), nullable=True)
    approval_decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    safe_error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    outcome: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    safe_skill_projection: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    round: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)


__all__ = ["RunToolCallModel"]
