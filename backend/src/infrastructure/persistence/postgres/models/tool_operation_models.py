"""Durable operation ledger for Worker side-effecting tool calls."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, JSON, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .legacy_orchestration_base import LegacyOrchestrationBase

_JSON_DOCUMENT = JSON().with_variant(JSONB(), "postgresql")


class WorkerToolOperationModel(LegacyOrchestrationBase):
    """Remember the first outcome for one fenced Worker tool operation."""

    __tablename__ = "worker_tool_operations"
    __table_args__ = (
        Index("idx_worker_tool_operations_execution", "execution_id"),
        Index("idx_worker_tool_operations_logical", "logical_operation_key"),
        Index(
            "idx_worker_tool_operations_incomplete",
            "status",
            "created_at",
            postgresql_where=text("status = 'executing'"),
        ),
    )

    operation_key: Mapped[str] = mapped_column(String, primary_key=True)
    logical_operation_key: Mapped[str] = mapped_column(String)
    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.run_id"))
    plan_id: Mapped[str] = mapped_column(ForeignKey("agent_plans.plan_id"))
    task_id: Mapped[str] = mapped_column(ForeignKey("agent_tasks.task_id"))
    execution_id: Mapped[str] = mapped_column(
        ForeignKey("task_executions.execution_id")
    )
    tool_name: Mapped[str] = mapped_column(String)
    tool_call_id: Mapped[str] = mapped_column(String)
    args_hash: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="executing")
    result_json: Mapped[dict[str, Any] | None] = mapped_column(
        _JSON_DOCUMENT, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
