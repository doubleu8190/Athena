"""Subagent 配置 PostgreSQL / SQLite ORM 映射。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from adapter.base import Base, json_column_type


class SubagentModel(Base):
    """管理员配置的 Subagent 持久化表。"""

    __tablename__ = "subagents"
    __table_args__ = (
        CheckConstraint("version >= 1", name="ck_subagents_version"),
        CheckConstraint(
            "max_llm_requests IS NULL OR (max_llm_requests >= 1 AND max_llm_requests <= 50)",
            name="ck_subagents_max_llm_requests",
        ),
        CheckConstraint(
            "timeout_seconds IS NULL OR (timeout_seconds >= 10 AND timeout_seconds <= 1800)",
            name="ck_subagents_timeout_seconds",
        ),
        CheckConstraint(
            "visibility_scope IN ('company', 'department')",
            name="ck_subagents_visibility_scope",
        ),
        CheckConstraint(
            "(visibility_scope = 'company' AND owner_department_id IS NULL) "
            "OR (visibility_scope = 'department' AND owner_department_id IS NOT NULL)",
            name="ck_subagents_owner_department_scope",
        ),
        Index(
            "uq_subagents_slug_active",
            "slug",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
            sqlite_where=text("deleted_at IS NULL"),
        ),
        Index("ix_subagents_enabled_created", "enabled", "created_at", "id"),
        Index(
            "ix_subagents_visibility_dept",
            "visibility_scope",
            "owner_department_id",
            "deleted_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(String(1000), nullable=False)
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    model_profile_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    allowed_tool_ids: Mapped[list[str]] = mapped_column(
        json_column_type(),
        nullable=False,
        default=list,
        server_default=text("'[]'"),
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
    )
    visibility_scope: Mapped[str] = mapped_column(
        String(16), nullable=False, default="company", server_default="company"
    )
    owner_department_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("departments.id", ondelete="RESTRICT"),
        nullable=True,
    )
    max_llm_requests: Mapped[int | None] = mapped_column(Integer, nullable=True)
    timeout_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default=text("1"),
    )
    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


__all__ = ["SubagentModel"]
