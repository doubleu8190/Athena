"""脚本执行附加记录 ORM 映射。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
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


class SkillExecutionModel(Base):
    """与 RunToolCallModel 一对一关联的脚本环境、日志和结果附加信息。"""

    __tablename__ = "skill_executions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'succeeded', 'failed', 'unknown')",
            name="ck_skill_executions_status",
        ),
        UniqueConstraint("tool_call_record_id", name="uq_skill_executions_tool_call"),
        ForeignKeyConstraint(
            ["run_id", "session_id"],
            ["runs.id", "runs.session_id"],
            name="fk_skill_executions_run_session",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tool_call_record_id"],
            ["run_tool_calls.id"],
            name="fk_skill_executions_tool_call_record",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["skill_id"],
            ["skills.id"],
            name="fk_skill_executions_skill",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["skill_version_id"],
            ["skill_versions.id"],
            name="fk_skill_executions_version",
            ondelete="RESTRICT",
        ),
        # 与迁移 20261123_0032 对齐：加速离线回收 CLI 按 log_object_key 核验日志对象引用。
        Index(
            "ix_skill_executions_log_object_key",
            "log_object_key",
            postgresql_where=text("log_object_key IS NOT NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(128), nullable=False)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    tool_call_record_id: Mapped[str] = mapped_column(String(128), nullable=False)
    skill_id: Mapped[str] = mapped_column(String(128), nullable=False)
    skill_version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    script_path: Mapped[str] = mapped_column(String(512), nullable=False)
    input_file_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    parameters_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    python_version: Mapped[str] = mapped_column(String(64), nullable=False)
    dependency_check: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    log_object_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    log_truncated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    log_error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    cleanup_error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    result_artifacts: Mapped[list[dict[str, object]]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


__all__ = ["SkillExecutionModel"]
