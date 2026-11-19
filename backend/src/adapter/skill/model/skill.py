"""Skill 身份与不可变版本 ORM 映射。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
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


def _json_type() -> JSON:
    """在 PostgreSQL 使用 JSONB，在 SQLite 测试中使用 JSON。"""
    return JSON().with_variant(JSONB(), "postgresql")


class SkillModel(Base):
    """稳定 Skill 身份、当前版本指针及管理员配置修订。"""

    __tablename__ = "skills"
    __table_args__ = (
        CheckConstraint("revision >= 1", name="ck_skills_revision"),
        CheckConstraint(
            "scope IN ('public', 'personal')",
            name="ck_skills_scope",
        ),
        CheckConstraint(
            "(scope = 'public' AND owner_id IS NULL AND deleted_at IS NULL) OR "
            "(scope = 'personal' AND owner_id IS NOT NULL)",
            name="ck_skills_owner_scope",
        ),
        Index(
            "uq_skills_public_slug",
            "slug",
            unique=True,
            postgresql_where=text("scope = 'public'"),
            sqlite_where=text("scope = 'public'"),
        ),
        Index(
            "uq_skills_personal_owner_slug",
            "owner_id",
            "slug",
            unique=True,
            postgresql_where=text("scope = 'personal' AND deleted_at IS NULL"),
            sqlite_where=text("scope = 'personal' AND deleted_at IS NULL"),
        ),
        Index(
            "ix_skills_owner_scope_enabled",
            "owner_id",
            "scope",
            "enabled",
            "deleted_at",
        ),
        ForeignKeyConstraint(
            ["current_version_id", "id"],
            ["skill_versions.id", "skill_versions.skill_id"],
            name="fk_skills_current_version",
            use_alter=True,
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["owner_id"],
            ["users.id"],
            name="fk_skills_owner",
            ondelete="RESTRICT",
        ),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    scope: Mapped[str] = mapped_column(
        String(16), nullable=False, default="public", server_default="public"
    )
    owner_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    current_version_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    script_requires_approval: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    revision: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SkillVersionModel(Base):
    """不可变 Skill 包版本正文、清单和依赖检查记录。"""

    __tablename__ = "skill_versions"
    __table_args__ = (
        CheckConstraint("ordinal > 0", name="ck_skill_versions_ordinal"),
        CheckConstraint(
            "request_operation IN "
            "('import', 'add_version', 'personal_create', 'personal_publish')",
            name="ck_skill_versions_request_operation",
        ),
        CheckConstraint(
            "(request_operation IN ('import', 'personal_create') "
            "AND request_target_skill_id IS NULL) OR "
            "(request_operation IN ('add_version', 'personal_publish') "
            "AND request_target_skill_id IS NOT NULL)",
            name="ck_skill_versions_request_target",
        ),
        UniqueConstraint("skill_id", "ordinal", name="uq_skill_versions_ordinal"),
        UniqueConstraint("object_key", name="uq_skill_versions_object_key"),
        UniqueConstraint("id", "skill_id", name="uq_skill_versions_id_skill"),
        Index("ix_skill_versions_package_sha256", "package_sha256"),
        UniqueConstraint(
            "created_by",
            "request_operation",
            "idempotency_key",
            name="uq_skill_versions_request_key",
        ),
        ForeignKeyConstraint(
            ["skill_id"],
            ["skills.id"],
            name="fk_skill_versions_skill",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name="fk_skill_versions_created_by",
            ondelete="RESTRICT",
        ),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    skill_id: Mapped[str] = mapped_column(String(128), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[str] = mapped_column(String(64), nullable=False)
    guide: Mapped[str] = mapped_column(Text, nullable=False)
    manifest: Mapped[dict[str, object]] = mapped_column(_json_type(), nullable=False)
    object_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    package_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    dependency_requirements: Mapped[list[str]] = mapped_column(
        _json_type(), nullable=False, default=list
    )
    dependency_check: Mapped[dict[str, object]] = mapped_column(_json_type(), nullable=False)
    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    request_operation: Mapped[str] = mapped_column(String(16), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    request_target_skill_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


__all__ = ["SkillModel", "SkillVersionModel"]
