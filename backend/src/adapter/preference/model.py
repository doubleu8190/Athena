"""个人工作偏好与显式记忆 PostgreSQL ORM 映射。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column

from adapter.base import Base


class UserWorkProfileModel(Base):
    """用户个人工作背景指令与显式记忆总开关表。"""

    __tablename__ = "user_work_profiles"

    user_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    custom_instructions: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="",
        server_default="",
    )
    memory_enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=true(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )


class UserMemoryModel(Base):
    """用户显式保存的轻量备忘记忆表。"""

    __tablename__ = "user_memories"
    __table_args__ = (
        CheckConstraint(
            "source IN ('manual', 'chat_explicit')",
            name="ck_user_memories_source",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    content: Mapped[str] = mapped_column(String(500), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


Index(
    "ix_user_memories_user_updated",
    UserMemoryModel.user_id,
    UserMemoryModel.updated_at.desc(),
    UserMemoryModel.id.desc(),
)

__all__ = ["UserMemoryModel", "UserWorkProfileModel"]
