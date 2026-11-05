"""最小 Session 归属模型。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from adapter.base import Base


class SessionModel(Base):
    """会话目录、所有者、项目绑定和会话级消息顺序。"""

    __tablename__ = "sessions"
    __table_args__ = (
        CheckConstraint(
            "last_message_sequence >= 0",
            name="ck_sessions_last_message_sequence",
        ),
        CheckConstraint(
            "title IS NULL OR length(title) <= 120",
            name="ck_sessions_title_length",
        ),
        ForeignKeyConstraint(
            ["project_id", "owner_id"],
            ["projects.id", "projects.owner_id"],
            name="fk_sessions_project_owner_projects",
            ondelete="RESTRICT",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("users.id", name="fk_sessions_owner_id_users"),
        nullable=False,
    )
    title: Mapped[str | None] = mapped_column(String(120), nullable=True)
    project_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_message_sequence: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
        server_default="0",
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deleted_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )


Index(
    "ix_sessions_owner_active_updated",
    SessionModel.owner_id,
    SessionModel.updated_at.desc(),
    SessionModel.id.desc(),
    postgresql_where=text("deleted_time IS NULL"),
)

Index(
    "ix_sessions_owner_project_active_updated",
    SessionModel.owner_id,
    SessionModel.project_id,
    SessionModel.updated_at.desc(),
    SessionModel.id.desc(),
    postgresql_where=text("deleted_time IS NULL"),
)

__all__ = ["SessionModel"]
