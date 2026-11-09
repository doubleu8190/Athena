"""Session 内部上下文摘要版本 ORM 映射。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from adapter.base import Base


class SessionContextSummaryModel(Base):
    """只保存完整压缩成功后的不可变摘要。"""

    __tablename__ = "session_context_summaries"
    __table_args__ = (
        UniqueConstraint("id", "session_id", name="uq_context_summaries_id_session"),
        UniqueConstraint("run_id", "operation_id", name="uq_context_summary_run_operation"),
        ForeignKeyConstraint(
            ["session_id", "owner_id"],
            ["sessions.id", "sessions.owner_id"],
            name="fk_context_summaries_session_owner",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["parent_summary_id", "session_id"],
            ["session_context_summaries.id", "session_context_summaries.session_id"],
            name="fk_context_summaries_parent_session",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["run_id", "session_id"],
            ["runs.id", "runs.session_id"],
            name="fk_context_summaries_run_session",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["policy_version_id"],
            ["llm_compression_policy_versions.id"],
            name="fk_context_summaries_policy",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["compression_model_version_id"],
            ["llm_model_profile_versions.id"],
            name="fk_context_summaries_model",
            ondelete="RESTRICT",
        ),
        Index(
            "ix_context_summaries_session_coverage",
            "session_id",
            "owner_id",
            "covered_through_sequence",
            "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    owner_id: Mapped[str] = mapped_column(String(128), nullable=False)
    covered_through_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    parent_summary_id: Mapped[str | None] = mapped_column(String(128))
    text: Mapped[str] = mapped_column(Text, nullable=False)
    run_id: Mapped[str] = mapped_column(String(128), nullable=False)
    execution_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False)
    operation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    policy_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    compression_model_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


__all__ = ["SessionContextSummaryModel"]
