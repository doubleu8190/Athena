"""统一三层权限引擎（resource_visibility_grants）、四大资产范围扩展与知识库复合外键拆除。"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261202_0035"
down_revision: str | None = "20261201_0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _drop_pg_fk_by_columns(
    table_name: str,
    referred_table: str,
    constrained_columns: Sequence[str],
) -> None:
    """按表名、目标表与有序外键列动态查找并删除 PostgreSQL 自动命名的复合外键。

    使用 PostgreSQL 匿名 DO 块，确保在线迁移与离线 --sql 输出均可安全执行，
    且仅作用于 current_schema() 内的约束，不依赖历史匿名外键截断命名。
    """
    col_array = ", ".join(f"'{col}'" for col in constrained_columns)
    op.execute(
        f"""
DO $$
DECLARE
    fk_name text;
BEGIN
    SELECT c.conname INTO fk_name
    FROM pg_constraint c
    JOIN pg_class rel ON rel.oid = c.conrelid
    JOIN pg_class ref ON ref.oid = c.confrelid
    WHERE c.contype = 'f'
      AND c.connamespace = current_schema()::regnamespace
      AND rel.relname = '{table_name}'
      AND ref.relname = '{referred_table}'
      AND (
          SELECT array_agg(a.attname::text ORDER BY u.ord)
          FROM unnest(c.conkey) WITH ORDINALITY AS u(attnum, ord)
          JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = u.attnum
      ) = ARRAY[{col_array}]
    LIMIT 1;
    IF fk_name IS NOT NULL THEN
        EXECUTE format(
            'ALTER TABLE %I.%I DROP CONSTRAINT %I',
            current_schema(),
            '{table_name}',
            fk_name
        );
    END IF;
END $$;
""".strip()
    )


def upgrade() -> None:
    """新建资源可见性授权表、拆除知识库单人复合外键并扩展四大资产三层范围列。"""
    # 1. 新建统一三层权限授权表 resource_visibility_grants。
    op.create_table(
        "resource_visibility_grants",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("resource_type", sa.String(length=32), nullable=False),
        sa.Column("resource_id", sa.String(length=128), nullable=False),
        sa.Column("target_type", sa.String(length=16), nullable=False),
        sa.Column("target_id", sa.String(length=64), nullable=False),
        sa.Column(
            "include_subdepartments",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "resource_type IN ('knowledge_base', 'skill', 'mcp_server', 'subagent')",
            name="ck_resource_visibility_grants_resource_type",
        ),
        sa.CheckConstraint(
            "target_type IN ('department', 'role')",
            name="ck_resource_visibility_grants_target_type",
        ),
        sa.UniqueConstraint(
            "resource_type",
            "resource_id",
            "target_type",
            "target_id",
            name="uq_resource_visibility_grant",
        ),
    )
    op.create_index(
        "ix_resource_visibility_grants_lookup",
        "resource_visibility_grants",
        ["resource_type", "target_type", "target_id"],
    )
    op.create_index(
        "ix_resource_visibility_grants_resource",
        "resource_visibility_grants",
        ["resource_type", "resource_id"],
    )

    # 2. 升级 knowledge_bases 三层范围字段，并拆除“单人私有”复合外键枷锁。
    op.add_column(
        "knowledge_bases",
        sa.Column(
            "visibility_scope",
            sa.String(length=16),
            nullable=False,
            server_default="personal",
        ),
    )
    op.add_column(
        "knowledge_bases",
        sa.Column("owner_department_id", sa.String(length=64), nullable=True),
    )
    op.create_foreign_key(
        "fk_knowledge_bases_owner_department",
        "knowledge_bases",
        "departments",
        ["owner_department_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_knowledge_bases_visibility_scope",
        "knowledge_bases",
        "visibility_scope IN ('company', 'department', 'personal')",
    )
    op.create_check_constraint(
        "ck_knowledge_bases_owner_department_scope",
        "knowledge_bases",
        "(visibility_scope = 'department' AND owner_department_id IS NOT NULL) "
        "OR (visibility_scope IN ('company', 'personal') AND owner_department_id IS NULL)",
    )
    op.create_index(
        "ix_knowledge_bases_visibility_dept",
        "knowledge_bases",
        ["visibility_scope", "owner_department_id", "deleted_time"],
    )

    # 2.1 knowledge_documents：拆除 (knowledge_base_id, owner_id) 复合外键，改为单列 FK。
    _drop_pg_fk_by_columns(
        "knowledge_documents",
        "knowledge_bases",
        ("knowledge_base_id", "owner_id"),
    )
    op.create_foreign_key(
        "fk_knowledge_documents_base",
        "knowledge_documents",
        "knowledge_bases",
        ["knowledge_base_id"],
        ["id"],
        ondelete="CASCADE",
    )

    # 2.2 knowledge_document_versions：拆除文档与来源两处复合外键。
    _drop_pg_fk_by_columns(
        "knowledge_document_versions",
        "knowledge_documents",
        ("document_id", "owner_id"),
    )
    _drop_pg_fk_by_columns(
        "knowledge_document_versions",
        "knowledge_sources",
        ("source_id", "owner_id"),
    )
    op.create_foreign_key(
        "fk_knowledge_versions_document",
        "knowledge_document_versions",
        "knowledge_documents",
        ["document_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_knowledge_versions_source",
        "knowledge_document_versions",
        "knowledge_sources",
        ["source_id"],
        ["id"],
    )

    # 2.3 project_knowledge_bases：保留项目侧复合 FK，拆除知识库侧复合 FK。
    _drop_pg_fk_by_columns(
        "project_knowledge_bases",
        "knowledge_bases",
        ("knowledge_base_id", "owner_id"),
    )
    op.create_foreign_key(
        "fk_project_knowledge_bases_base",
        "project_knowledge_bases",
        "knowledge_bases",
        ["knowledge_base_id"],
        ["id"],
        ondelete="CASCADE",
    )

    # 3. 升级 skills 表：将历史 public 迁移为 company，新增 owner_department_id 与部门唯一索引。
    op.drop_constraint("ck_skills_owner_scope", "skills", type_="check")
    op.drop_constraint("ck_skills_scope", "skills", type_="check")
    op.drop_index("uq_skills_public_slug", table_name="skills")

    op.execute("UPDATE skills SET scope = 'company' WHERE scope = 'public'")
    op.alter_column(
        "skills",
        "scope",
        existing_type=sa.String(length=16),
        nullable=False,
        server_default="company",
    )
    op.add_column(
        "skills",
        sa.Column("owner_department_id", sa.String(length=64), nullable=True),
    )
    op.create_foreign_key(
        "fk_skills_owner_department",
        "skills",
        "departments",
        ["owner_department_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_skills_scope",
        "skills",
        "scope IN ('company', 'department', 'personal')",
    )
    op.create_check_constraint(
        "ck_skills_owner_scope",
        "skills",
        "(scope = 'company' AND owner_id IS NULL AND owner_department_id IS NULL "
        "AND deleted_at IS NULL) OR "
        "(scope = 'department' AND owner_id IS NULL AND owner_department_id IS NOT NULL) OR "
        "(scope = 'personal' AND owner_id IS NOT NULL AND owner_department_id IS NULL)",
    )
    op.create_index(
        "uq_skills_company_slug",
        "skills",
        ["slug"],
        unique=True,
        postgresql_where=sa.text("scope = 'company'"),
    )
    op.create_index(
        "uq_skills_department_slug",
        "skills",
        ["owner_department_id", "slug"],
        unique=True,
        postgresql_where=sa.text("scope = 'department' AND deleted_at IS NULL"),
    )
    op.create_index(
        "ix_skills_department_scope",
        "skills",
        ["owner_department_id", "scope", "enabled", "deleted_at"],
    )

    # 4. 升级 mcp_servers 表：新增 visibility_scope（默认 company）与 owner_department_id。
    op.add_column(
        "mcp_servers",
        sa.Column(
            "visibility_scope",
            sa.String(length=16),
            nullable=False,
            server_default="company",
        ),
    )
    op.add_column(
        "mcp_servers",
        sa.Column("owner_department_id", sa.String(length=64), nullable=True),
    )
    op.create_foreign_key(
        "fk_mcp_servers_owner_department",
        "mcp_servers",
        "departments",
        ["owner_department_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_mcp_servers_visibility_scope",
        "mcp_servers",
        "visibility_scope IN ('company', 'department')",
    )
    op.create_check_constraint(
        "ck_mcp_servers_owner_department_scope",
        "mcp_servers",
        "(visibility_scope = 'company' AND owner_department_id IS NULL) "
        "OR (visibility_scope = 'department' AND owner_department_id IS NOT NULL)",
    )
    op.create_index(
        "ix_mcp_servers_visibility_dept",
        "mcp_servers",
        ["visibility_scope", "owner_department_id"],
    )

    # 5. 升级 subagents 表：新增 visibility_scope（默认 company）与 owner_department_id。
    op.add_column(
        "subagents",
        sa.Column(
            "visibility_scope",
            sa.String(length=16),
            nullable=False,
            server_default="company",
        ),
    )
    op.add_column(
        "subagents",
        sa.Column("owner_department_id", sa.String(length=64), nullable=True),
    )
    op.create_foreign_key(
        "fk_subagents_owner_department",
        "subagents",
        "departments",
        ["owner_department_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_subagents_visibility_scope",
        "subagents",
        "visibility_scope IN ('company', 'department')",
    )
    op.create_check_constraint(
        "ck_subagents_owner_department_scope",
        "subagents",
        "(visibility_scope = 'company' AND owner_department_id IS NULL) "
        "OR (visibility_scope = 'department' AND owner_department_id IS NOT NULL)",
    )
    op.create_index(
        "ix_subagents_visibility_dept",
        "subagents",
        ["visibility_scope", "owner_department_id", "deleted_at"],
    )


def downgrade() -> None:
    """回滚统一三层权限架构与知识库单列外键改动。"""
    # 1. 回滚 subagents 表新增索引、约束、外键与列。
    op.drop_index("ix_subagents_visibility_dept", table_name="subagents")
    op.drop_constraint("ck_subagents_owner_department_scope", "subagents", type_="check")
    op.drop_constraint("ck_subagents_visibility_scope", "subagents", type_="check")
    op.drop_constraint("fk_subagents_owner_department", "subagents", type_="foreignkey")
    op.drop_column("subagents", "owner_department_id")
    op.drop_column("subagents", "visibility_scope")

    # 2. 回滚 mcp_servers 表新增索引、约束、外键与列。
    op.drop_index("ix_mcp_servers_visibility_dept", table_name="mcp_servers")
    op.drop_constraint("ck_mcp_servers_owner_department_scope", "mcp_servers", type_="check")
    op.drop_constraint("ck_mcp_servers_visibility_scope", "mcp_servers", type_="check")
    op.drop_constraint("fk_mcp_servers_owner_department", "mcp_servers", type_="foreignkey")
    op.drop_column("mcp_servers", "owner_department_id")
    op.drop_column("mcp_servers", "visibility_scope")

    # 3. 回滚 skills 表：将 company 恢复为 public，移除 department 索引与外键列。
    op.drop_index("ix_skills_department_scope", table_name="skills")
    op.drop_index("uq_skills_department_slug", table_name="skills")
    op.drop_index("uq_skills_company_slug", table_name="skills")
    op.drop_constraint("ck_skills_owner_scope", "skills", type_="check")
    op.drop_constraint("ck_skills_scope", "skills", type_="check")
    op.drop_constraint("fk_skills_owner_department", "skills", type_="foreignkey")
    op.drop_column("skills", "owner_department_id")
    op.execute("UPDATE skills SET scope = 'public' WHERE scope = 'company'")
    op.alter_column(
        "skills",
        "scope",
        existing_type=sa.String(length=16),
        nullable=False,
        server_default="public",
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
    op.create_index(
        "uq_skills_public_slug",
        "skills",
        ["slug"],
        unique=True,
        postgresql_where=sa.text("scope = 'public'"),
    )

    # 4. 恢复知识库历史复合外键并移除 knowledge_bases 三层范围列。
    op.drop_constraint(
        "fk_project_knowledge_bases_base",
        "project_knowledge_bases",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "project_knowledge_bases_knowledge_base_id_owner_id_fkey",
        "project_knowledge_bases",
        "knowledge_bases",
        ["knowledge_base_id", "owner_id"],
        ["id", "owner_id"],
        ondelete="CASCADE",
    )

    op.drop_constraint(
        "fk_knowledge_versions_source",
        "knowledge_document_versions",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_knowledge_versions_document",
        "knowledge_document_versions",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "knowledge_document_versions_source_id_owner_id_fkey",
        "knowledge_document_versions",
        "knowledge_sources",
        ["source_id", "owner_id"],
        ["id", "owner_id"],
    )
    op.create_foreign_key(
        "knowledge_document_versions_document_id_owner_id_fkey",
        "knowledge_document_versions",
        "knowledge_documents",
        ["document_id", "owner_id"],
        ["id", "owner_id"],
        ondelete="CASCADE",
    )

    op.drop_constraint("fk_knowledge_documents_base", "knowledge_documents", type_="foreignkey")
    op.create_foreign_key(
        "knowledge_documents_knowledge_base_id_owner_id_fkey",
        "knowledge_documents",
        "knowledge_bases",
        ["knowledge_base_id", "owner_id"],
        ["id", "owner_id"],
        ondelete="CASCADE",
    )

    op.drop_index("ix_knowledge_bases_visibility_dept", table_name="knowledge_bases")
    op.drop_constraint(
        "ck_knowledge_bases_owner_department_scope",
        "knowledge_bases",
        type_="check",
    )
    op.drop_constraint(
        "ck_knowledge_bases_visibility_scope",
        "knowledge_bases",
        type_="check",
    )
    op.drop_constraint("fk_knowledge_bases_owner_department", "knowledge_bases", type_="foreignkey")
    op.drop_column("knowledge_bases", "owner_department_id")
    op.drop_column("knowledge_bases", "visibility_scope")

    # 5. 删除 resource_visibility_grants 表及索引。
    op.drop_index("ix_resource_visibility_grants_resource", table_name="resource_visibility_grants")
    op.drop_index("ix_resource_visibility_grants_lookup", table_name="resource_visibility_grants")
    op.drop_table("resource_visibility_grants")
