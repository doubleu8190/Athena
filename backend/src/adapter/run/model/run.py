"""Run PostgreSQL ORM 映射。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from adapter.base import Base


def _json_type() -> JSON:
    """在 PostgreSQL 使用 JSONB，在 SQLite 测试中使用 JSON。"""
    return JSON().with_variant(JSONB(), "postgresql")


class RunModel(Base):
    """Run 当前状态和执行租约。"""

    __tablename__ = "runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'waiting_for_input', "
            "'waiting_for_approval', 'cancelling', 'completed', 'failed', 'cancelled')",
            name="ck_runs_status",
        ),
        CheckConstraint("context_cutoff_sequence >= 0", name="ck_runs_context_cutoff"),
        CheckConstraint("event_sequence >= 0", name="ck_runs_event_sequence"),
        CheckConstraint("execution_epoch >= 0", name="ck_runs_execution_epoch"),
        CheckConstraint(
            "llm_request_count >= 0 AND llm_request_count <= 40",
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
                "status IN ('queued', 'running', 'waiting_for_input', "
                "'waiting_for_approval', 'cancelling')"
            ),
            sqlite_where=text(
                "status IN ('queued', 'running', 'waiting_for_input', "
                "'waiting_for_approval', 'cancelling')"
            ),
        ),
        Index("ix_runs_claim", "status", "queued_at", "id"),
        Index("ix_runs_lease", "status", "lease_expires_at"),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    owner_id: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    trigger_message_id: Mapped[str] = mapped_column(String(64), nullable=False)
    context_cutoff_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    retry_of_run_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    model_config_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    main_prompt_version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    compression_prompt_version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    compression_policy_version_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    skill_snapshot: Mapped[dict[str, object] | None] = mapped_column(
        _json_type(), nullable=True
    )
    input_file_ids: Mapped[list[str]] = mapped_column(
        _json_type(),
        nullable=False,
        default=list,
        server_default=text("'[]'"),
    )
    tool_snapshot: Mapped[dict[str, object] | None] = mapped_column(_json_type(), nullable=True)
    auth_session_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    llm_request_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    prompt_template_version: Mapped[str] = mapped_column(String(64), nullable=False)
    event_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    execution_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
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
    usage: Mapped[dict[str, object]] = mapped_column(_json_type(), nullable=False, default=dict)


__all__ = ["RunModel"]
