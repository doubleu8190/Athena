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
    # 压缩相关字段
    compression_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_compressed_message_id: Mapped[str | None] = mapped_column(
        String, nullable=True
    )
    last_summarized_message_id: Mapped[str | None] = mapped_column(
        String, nullable=True
    )
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
    run_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # 平铺自 metadata_json 的字段
    step_id: Mapped[str | None] = mapped_column(String, nullable=True)
    tool_call_record_id: Mapped[str | None] = mapped_column(String, nullable=True)
    tool_name: Mapped[str | None] = mapped_column(String, nullable=True)
    type: Mapped[str | None] = mapped_column(String, nullable=True)
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
    parent_run_id: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String)
    started_at: Mapped[str] = mapped_column(String)
    completed_at: Mapped[str | None] = mapped_column(String, nullable=True)
    duration_ms: Mapped[float] = mapped_column(Float, default=0)
    llm_input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    llm_output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
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
    __table_args__ = (Index("idx_approval_logs_session", "session_id"),)

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


class MemoryModel(Base):
    """记忆表模型 — 与 ChromaDB 双写的 SQLite 侧.

    用于 FTS5 全文检索（关键词检索），ChromaDB 负责向量检索。
    content 字段通过 FTS5 虚拟表 memory_fts 建立全文索引。
    """

    __tablename__ = "memories"
    __table_args__ = (Index("idx_memories_session", "session_id"),)

    id: Mapped[str] = mapped_column(String, primary_key=True)
    session_id: Mapped[str] = mapped_column(String)
    content: Mapped[str] = mapped_column(Text)
    # 仅存任意用户扩展字段（系统/语义字段一律拆为独立列，避免双源真相）
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    pinned: Mapped[int] = mapped_column(Integer, default=0)  # 0=未固定, 1=固定
    expires_at: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String)
    last_accessed: Mapped[str | None] = mapped_column(String, nullable=True)
    access_count: Mapped[int] = mapped_column(Integer, default=0)
    # 语义字段（平铺自 metadata_json，支持 SQL 过滤）
    type: Mapped[str | None] = mapped_column(String, nullable=True)  # fact / summary
    category: Mapped[str | None] = mapped_column(String, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str | None] = mapped_column(
        String, nullable=True
    )  # extraction / threshold / api
    deleted_time: Mapped[str | None] = mapped_column(String, nullable=True)


# FTS5 虚拟表 DDL（SQLAlchemy ORM 不支持 FTS5，需通过原生 SQL 创建）
MEMORY_FTS_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    content,
    memory_id UNINDEXED,
    tokenize='unicode61'
)
"""
