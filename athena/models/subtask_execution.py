"""SubtaskExecution model — subtask_executions table.

Records per-step execution details including retry count, fallback chain,
idempotency keys, and token usage. Used for audit and fault recovery.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, Integer, String, Text, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from athena.models.base import Base


class SubtaskExecution(Base):
    __tablename__ = "subtask_executions"

    execution_id: Mapped[str] = mapped_column(String, primary_key=True)
    task_id: Mapped[str] = mapped_column(
        String, ForeignKey("tasks.task_id"), nullable=False, index=True
    )
    step: Mapped[int] = mapped_column(Integer, nullable=False)
    tool_name: Mapped[str | None] = mapped_column(String, nullable=True)
    mcp_server_id: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(
        String, default="pending"
    )  # 'pending', 'running', 'success', 'failed', 'timeout',
    # 'timeout_cancelled', 'user_rejected', 'skipped', 'fallback_used'
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    is_recovered: Mapped[bool] = mapped_column(Boolean, default=False)
    fallback_used: Mapped[bool] = mapped_column(Boolean, default=False)
    fallback_from: Mapped[str | None] = mapped_column(String, nullable=True)
    fallback_depth: Mapped[int] = mapped_column(Integer, default=0)
    idempotency_key: Mapped[str | None] = mapped_column(
        String, nullable=True, index=True
    )  # Format: {task_id}:{step}:{retry_count}
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)
    input_args: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON
    output_preview: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_usage_json: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON
