"""项目 PostgreSQL ORM 映射。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from adapter.base import Base


def _json_type() -> JSON:
    """在 PostgreSQL 使用 JSONB，在 SQLite 测试中使用 JSON。"""
    return JSON().with_variant(JSONB(), "postgresql")


class ProjectModel(Base):
    """项目目录表。"""

    __tablename__ = "projects"
    __table_args__ = (UniqueConstraint("id", "owner_id", name="uq_projects_id_owner"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("users.id", name="fk_projects_owner_id_users"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    default_skill_ids: Mapped[list[str]] = mapped_column(
        _json_type(),
        nullable=False,
        default=list,
        server_default=text("'[]'"),
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deleted_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )


Index(
    "ix_projects_owner_active_updated",
    ProjectModel.owner_id,
    ProjectModel.updated_at.desc(),
    ProjectModel.id.desc(),
    postgresql_where=text("deleted_time IS NULL"),
)

__all__ = ["ProjectModel"]
