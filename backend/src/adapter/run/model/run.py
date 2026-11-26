"""Run PostgreSQL ORM 映射。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from adapter.base import Base


class RunModel(Base):
    """Run 当前状态和执行租约。"""

    __tablename__ = "runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'waiting_for_input', "
            "'waiting_for_approval', 'waiting_for_subagents', 'cancelling', "
            "'completed', 'failed', 'cancelled')",
            name="ck_runs_status",
        ),
        CheckConstraint("context_cutoff_sequence >= 0", name="ck_runs_context_cutoff"),
        CheckConstraint("event_sequence >= 0", name="ck_runs_event_sequence"),
        CheckConstraint("execution_epoch >= 0", name="ck_runs_execution_epoch"),
        CheckConstraint(
            "llm_request_count >= 0 AND llm_request_count <= 50",
            name="ck_runs_llm_request_count",
        ),
        ForeignKeyConstraint(
            ["session_id", "owner_id"],
            ["sessions.id", "sessions.owner_id"],
            name="fk_runs_session_owner",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["trigger_message_id", "session_id"],
            ["conversation_messages.id", "conversation_messages.session_id"],
            name="fk_runs_trigger_message_session",
            deferrable=True,
            initially="DEFERRED",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["report_message_id", "session_id"],
            ["conversation_messages.id", "conversation_messages.session_id"],
            name="fk_runs_report_message_session",
            deferrable=True,
            initially="DEFERRED",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["retry_of_run_id", "session_id"],
            ["runs.id", "runs.session_id"],
            name="fk_runs_retry_session",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("id", "session_id", name="uq_runs_id_session_pair"),
        ForeignKeyConstraint(
            ["model_config_version_id"],
            ["llm_model_profile_versions.id"],
            name="fk_runs_model_profile",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["main_prompt_version_id"],
            ["system_prompt_versions.id"],
            name="fk_runs_main_prompt_version",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["compression_prompt_version_id"],
            ["system_prompt_versions.id"],
            name="fk_runs_compression_prompt_version",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["compression_policy_version_id"],
            ["llm_compression_policy_versions.id"],
            name="fk_runs_compression_policy",
            ondelete="RESTRICT",
        ),
        Index(
            "uq_runs_active_session",
            "session_id",
            unique=True,
            postgresql_where=text(
                "depth = 0 AND status IN ('queued', 'running', 'waiting_for_input', "
                "'waiting_for_approval', 'waiting_for_subagents', 'cancelling')"
            ),
        ),
        Index("ix_runs_claim", "status", "queued_at", "id"),
        Index("ix_runs_lease", "status", "lease_expires_at"),
        # 加速按会话分页倒序列出 Run 及按会话检查活跃 Run 状态的高频查询。
        Index(
            "ix_runs_session_created",
            "session_id",
            text("created_at DESC"),
            text("id DESC"),
        ),
        Index("ix_runs_session_status", "session_id", "status"),
        Index("ix_runs_parent_run_id", "parent_run_id"),
        Index("ix_runs_root_run_id", "root_run_id"),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    owner_id: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    trigger_message_id: Mapped[str] = mapped_column(String(64), nullable=False)
    context_cutoff_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    retry_of_run_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    model_config_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # 固化创建 Run 时的非密钥模型元数据快照，严禁存放任何明文或密文 api_key。
    model_snapshot_json: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    main_prompt_version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    compression_prompt_version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    compression_policy_version_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    skill_snapshot: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    input_file_ids: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default=text("'[]'"),
    )
    # NULL 表示默认企业工作台范围；[] 表示显式关闭知识库检索；非空表示显式限定知识库 ID 列表。
    knowledge_base_ids: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    tool_snapshot: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    auth_session_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    llm_request_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    prompt_template_version: Mapped[str] = mapped_column(String(64), nullable=False)
    event_sequence: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    execution_epoch: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    worker_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    execution_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    execution_deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    queued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    first_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    safe_error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    report_message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    usage: Mapped[dict[str, object]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    # Phase 2 Subagent 编排预留字段，默认兼容主 Run（depth=0）。
    parent_run_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    root_run_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    parent_tool_call_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    depth: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    subagent_profile_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    subagent_slug: Mapped[str | None] = mapped_column(String(64), nullable=True)
    subagent_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    subagent_system_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    max_llm_requests: Mapped[int | None] = mapped_column(Integer, nullable=True)
    timeout_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)


__all__ = ["RunModel"]
