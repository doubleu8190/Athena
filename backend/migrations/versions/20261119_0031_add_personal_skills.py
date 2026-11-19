"""Add owner-scoped personal Skills while preserving public Skill behavior."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261119_0031"
down_revision: str | None = "20261129_0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """新增归属、软删除范围与个人请求操作，并放开跨范围内容哈希唯一限制。"""
    op.add_column(
        "skills",
        sa.Column(
            "scope",
            sa.String(length=16),
            nullable=False,
            server_default="public",
        ),
    )
    op.add_column("skills", sa.Column("owner_id", sa.String(length=128)))
    op.add_column(
        "skills",
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
    )
    op.create_check_constraint(
        "ck_skills_scope",
        "skills",
        "scope IN ('public', 'personal')",
    )
    op.create_check_constraint(
        "ck_skills_owner_scope",
        "skills",
        "(scope = 'public' AND owner_id IS NULL AND deleted_at IS NULL) OR "
        "(scope = 'personal' AND owner_id IS NOT NULL)",
    )
    op.create_foreign_key(
        "fk_skills_owner",
        "skills",
        "users",
        ["owner_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.drop_constraint("uq_skills_slug", "skills", type_="unique")
    op.create_index(
        "uq_skills_public_slug",
        "skills",
        ["slug"],
        unique=True,
        postgresql_where=sa.text("scope = 'public'"),
    )
    op.create_index(
        "uq_skills_personal_owner_slug",
        "skills",
        ["owner_id", "slug"],
        unique=True,
        postgresql_where=sa.text("scope = 'personal' AND deleted_at IS NULL"),
    )
    op.create_index(
        "ix_skills_owner_scope_enabled",
        "skills",
        ["owner_id", "scope", "enabled", "deleted_at"],
    )

    op.drop_constraint(
        "ck_skill_versions_request_operation",
        "skill_versions",
        type_="check",
    )
    op.create_check_constraint(
        "ck_skill_versions_request_operation",
        "skill_versions",
        "request_operation IN "
        "('import', 'add_version', 'personal_create', 'personal_publish')",
    )
    op.drop_constraint(
        "ck_skill_versions_request_target",
        "skill_versions",
        type_="check",
    )
    op.create_check_constraint(
        "ck_skill_versions_request_target",
        "skill_versions",
        "(request_operation IN ('import', 'personal_create') "
        "AND request_target_skill_id IS NULL) OR "
        "(request_operation IN ('add_version', 'personal_publish') "
        "AND request_target_skill_id IS NOT NULL)",
    )
    op.drop_constraint(
        "uq_skill_versions_package_hash",
        "skill_versions",
        type_="unique",
    )
    op.create_index(
        "ix_skill_versions_package_sha256",
        "skill_versions",
        ["package_sha256"],
    )


def downgrade() -> None:
    """拒绝可能丢失归属或合并重复内容的降级，避免自动破坏个人 Skill 数据。"""
    connection = op.get_bind()
    personal_count = int(
        connection.scalar(
            sa.text("SELECT count(*) FROM skills WHERE scope = 'personal'")
        )
        or 0
    )
    if personal_count:
        raise RuntimeError("personal_skills_present_manual_cleanup_required")
    duplicate_hash = connection.scalar(
        sa.text(
            "SELECT package_sha256 FROM skill_versions "
            "GROUP BY package_sha256 HAVING count(*) > 1 LIMIT 1"
        )
    )
    if duplicate_hash is not None:
        raise RuntimeError("skill_package_hash_duplicates_prevent_downgrade")

    op.drop_index(
        "ix_skill_versions_package_sha256",
        table_name="skill_versions",
    )
    op.create_unique_constraint(
        "uq_skill_versions_package_hash",
        "skill_versions",
        ["package_sha256"],
    )
    op.drop_constraint(
        "ck_skill_versions_request_target",
        "skill_versions",
        type_="check",
    )
    op.create_check_constraint(
        "ck_skill_versions_request_target",
        "skill_versions",
        "(request_operation = 'import' AND request_target_skill_id IS NULL) OR "
        "(request_operation = 'add_version' AND request_target_skill_id IS NOT NULL)",
    )
    op.drop_constraint(
        "ck_skill_versions_request_operation",
        "skill_versions",
        type_="check",
    )
    op.create_check_constraint(
        "ck_skill_versions_request_operation",
        "skill_versions",
        "request_operation IN ('import', 'add_version')",
    )

    op.drop_index("ix_skills_owner_scope_enabled", table_name="skills")
    op.drop_index("uq_skills_personal_owner_slug", table_name="skills")
    op.drop_index("uq_skills_public_slug", table_name="skills")
    op.create_unique_constraint("uq_skills_slug", "skills", ["slug"])
    op.drop_constraint("fk_skills_owner", "skills", type_="foreignkey")
    op.drop_constraint("ck_skills_owner_scope", "skills", type_="check")
    op.drop_constraint("ck_skills_scope", "skills", type_="check")
    op.drop_column("skills", "deleted_at")
    op.drop_column("skills", "owner_id")
    op.drop_column("skills", "scope")
