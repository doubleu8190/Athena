"""PostgreSQL 模型：Worker 单次 execution 的模型消息和工具回执。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base

_JSON_DOCUMENT = JSON().with_variant(JSONB(), "postgresql")


class WorkerExecutionMessageModel(Base):
    """Worker 实际模型消息和独立工具结果回执的顺序记录。"""

    __tablename__ = "worker_execution_messages"
    __table_args__ = (
        CheckConstraint(
            "(entry_type = 'llm_message' AND role IS NOT NULL "
            "AND role IN ('system', 'user', 'assistant')) "
            "OR (entry_type = 'tool_result' AND role IS NULL)",
            name="ck_worker_execution_messages_role",
        ),
        UniqueConstraint(
            "task_id",
            "execution_generation",
            "sequence_no",
            name="uq_worker_execution_messages_sequence",
        ),
        Index(
            "idx_worker_execution_messages_run",
            "run_id",
            "sequence_no",
        ),
        Index(
            "idx_worker_execution_messages_tool_call",
            "tool_call_id",
            postgresql_where=text("tool_call_id IS NOT NULL"),
        ),
    )

    message_id: Mapped[str] = mapped_column(String, primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"))
    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.run_id"))
    plan_id: Mapped[str] = mapped_column(ForeignKey("agent_plans.plan_id"))
    task_id: Mapped[str] = mapped_column(ForeignKey("agent_tasks.task_id"))
    execution_id: Mapped[str] = mapped_column(
        ForeignKey("task_executions.execution_id")
    )
    execution_generation: Mapped[int] = mapped_column(Integer)
    invocation_id: Mapped[str] = mapped_column(String)
    sequence_no: Mapped[int] = mapped_column(Integer)
    entry_type: Mapped[str] = mapped_column(String)
    role: Mapped[str | None] = mapped_column(String, nullable=True)
    content_json: Mapped[dict[str, object]] = mapped_column(_JSON_DOCUMENT)
    tool_call_id: Mapped[str | None] = mapped_column(String, nullable=True)
    tool_name: Mapped[str | None] = mapped_column(String, nullable=True)
    operation_key: Mapped[str | None] = mapped_column(String, nullable=True)
    result_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


__all__ = ["WorkerExecutionMessageModel"]
