"""管理员系统提示词版本及当前指针 ORM 映射。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from adapter.base import Base


class SystemPromptVersionModel(Base):
    """按类别保存不可修改、不可删除的系统提示词正文。"""

    __tablename__ = "system_prompt_versions"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('main', 'compression')",
            name="ck_system_prompt_versions_kind",
        ),
        UniqueConstraint("id", "kind", name="uq_system_prompt_versions_id_kind"),
        ForeignKeyConstraint(
            ["restored_from_version_id", "kind"],
            ["system_prompt_versions.id", "system_prompt_versions.kind"],
            name="fk_system_prompt_versions_restored_same_kind",
            ondelete="RESTRICT",
        ),
        Index(
            "ix_system_prompt_versions_kind_created_id",
            "kind",
            "created_at",
            "id",
        ),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    restored_from_version_id: Mapped[str | None] = mapped_column(String(128), nullable=True)


class SystemPromptSettingModel(Base):
    """每类提示词恰好维护一条当前生效版本指针。"""

    __tablename__ = "system_prompt_settings"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('main', 'compression')",
            name="ck_system_prompt_settings_kind",
        ),
        ForeignKeyConstraint(
            ["current_version_id", "kind"],
            ["system_prompt_versions.id", "system_prompt_versions.kind"],
            name="fk_system_prompt_settings_current_same_kind",
            ondelete="RESTRICT",
        ),
    )

    kind: Mapped[str] = mapped_column(String(16), primary_key=True)
    current_version_id: Mapped[str] = mapped_column(String(128), nullable=False)


__all__ = ["SystemPromptSettingModel", "SystemPromptVersionModel"]
