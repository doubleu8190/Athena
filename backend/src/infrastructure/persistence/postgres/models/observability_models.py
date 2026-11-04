"""用户进度与后端链路追踪的 PostgreSQL 模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class TraceSpanModel(Base):
    """保存一条可重建调用树的 span 记录。"""

    __tablename__ = "trace_spans"
    __table_args__ = (
        UniqueConstraint("run_id", "sequence", name="uq_trace_spans_run_sequence"),
        Index("idx_trace_spans_run_parent", "run_id", "parent_span_id"),
        Index("idx_trace_spans_session", "session_id", "started_at"),
    )

    span_id: Mapped[str] = mapped_column(String, primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), nullable=False)
    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.run_id"), nullable=False)
    parent_span_id: Mapped[str | None] = mapped_column(
        ForeignKey("trace_spans.span_id"), nullable=True
    )
    span_type: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(String(240), nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    attributes_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    error_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)


class RunProgressModel(Base):
    """保存不可变的用户可见执行过程消息。"""

    __tablename__ = "run_progress_entries"
    __table_args__ = (
        UniqueConstraint("scope_key", "sequence", name="uq_run_progress_scope_sequence"),
        Index("idx_run_progress_run", "run_id", "ingest_sequence"),
        Index("idx_run_progress_execution", "execution_id", "sequence"),
        Index("idx_run_progress_session_ingest", "session_id", "ingest_sequence"),
    )

    progress_id: Mapped[str] = mapped_column(String, primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), nullable=False)
    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.run_id"), nullable=False)
    execution_id: Mapped[str | None] = mapped_column(
        ForeignKey("task_executions.execution_id"), nullable=True
    )
    task_id: Mapped[str | None] = mapped_column(String, nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String, nullable=True)
    source: Mapped[str] = mapped_column(String, nullable=False)
    scope_key: Mapped[str] = mapped_column(String, nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ingest_sequence: Mapped[int] = mapped_column(
        BigInteger, Identity(), nullable=False, unique=True
    )


__all__ = ["RunProgressModel", "TraceSpanModel"]
