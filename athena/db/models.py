"""SQLAlchemy ORM 模型定义.

所有模型采用软删除策略，包含 deleted_time 字段。
不使用 relationship()，级联操作在 Repository 层手动处理。
"""

from __future__ import annotations

from sqlalchemy import Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """ORM 模型基类."""
    pass


class SessionModel(Base):
    """会话表模型."""

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    title: Mapped[str] = mapped_column(String, default="New Session")
    status: Mapped[str] = mapped_column(String, default="idle")
    run_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String)
    updated_at: Mapped[str] = mapped_column(String)
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    deleted_time: Mapped[str | None] = mapped_column(String, nullable=True)


class MessageModel(Base):
    """消息表模型."""

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
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    timestamp: Mapped[str] = mapped_column(String)
    deleted_time: Mapped[str | None] = mapped_column(String, nullable=True)


class StepModel(Base):
    """执行步骤表模型."""

    __tablename__ = "steps"
    __table_args__ = (
        Index("idx_steps_session", "session_id"),
        Index("idx_steps_run", "run_id"),
        Index("idx_steps_number", "step_number"),
        Index("idx_steps_parent", "parent_step_id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"))
    run_id: Mapped[str] = mapped_column(String)
    step_number: Mapped[int] = mapped_column(Integer)
    step_type: Mapped[str] = mapped_column(String)
    parent_step_id: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String)
    started_at: Mapped[str] = mapped_column(String)
    completed_at: Mapped[str | None] = mapped_column(String, nullable=True)
    duration_ms: Mapped[float] = mapped_column(Float, default=0)
    llm_input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    llm_output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    deleted_time: Mapped[str | None] = mapped_column(String, nullable=True)


class ToolCallModel(Base):
    """工具调用记录表模型."""

    __tablename__ = "tool_call"
    __table_args__ = (
        Index("idx_tool_call_session", "session_id"),
        Index("idx_tool_call_step", "step_id"),
        Index("idx_tool_call_status", "status"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"))
    step_id: Mapped[str] = mapped_column(ForeignKey("steps.id"))
    tool_name: Mapped[str] = mapped_column(String)
    arguments_json: Mapped[str] = mapped_column(Text, default="{}")
    raw_output: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String)
    started_at: Mapped[str] = mapped_column(String)
    completed_at: Mapped[str | None] = mapped_column(String, nullable=True)
    duration_ms: Mapped[float] = mapped_column(Float, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_stack: Mapped[str | None] = mapped_column(Text, nullable=True)
    deleted_time: Mapped[str | None] = mapped_column(String, nullable=True)


class ApprovalLogModel(Base):
    """审批日志表模型."""

    __tablename__ = "approval_logs"
    __table_args__ = (
        Index("idx_approval_logs_session", "session_id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"))
    tool_call_id: Mapped[str] = mapped_column(String)
    tool_name: Mapped[str] = mapped_column(String)
    arguments_json: Mapped[str] = mapped_column(Text, default="{}")
    risk_level: Mapped[str] = mapped_column(String)
    decision: Mapped[str] = mapped_column(String)
    decision_time_ms: Mapped[float] = mapped_column(Float, default=0)
    timestamp: Mapped[str] = mapped_column(String)
    deleted_time: Mapped[str | None] = mapped_column(String, nullable=True)
