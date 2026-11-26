"""Athena v2 初始 PostgreSQL 基线 Schema 与系统提示词种子数据。"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

from domain.system_prompt.defaults import (
    INITIAL_COMPRESSION_SYSTEM_PROMPT,
    INITIAL_MAIN_SYSTEM_PROMPT,
    INITIAL_SYSTEM_PROMPT_IDS,
)

revision: str = "0001_initial_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """安装 pgvector 扩展，创建全部 39 张基线表、索引、外键约束并写入系统提示词种子。"""
    # 1. 确保 pgvector 扩展已安装，支撑知识库分块向量列与 HNSW 索引。
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # 2. 组织架构、用户认证与权限基础表。
    op.create_table(
        "departments",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("external_id", sa.String(128), nullable=False),
        sa.Column(
            "parent_id",
            sa.String(64),
            sa.ForeignKey("departments.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("path", sa.String(2048), nullable=False),
        sa.Column("order_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("external_id", name="uq_departments_external_id"),
        sa.CheckConstraint("status IN ('active', 'archived')", name="ck_departments_status"),
    )
    op.create_index("ix_departments_path", "departments", ["path"])
    op.create_index(
        "ix_departments_path_pattern",
        "departments",
        ["path"],
        postgresql_ops={"path": "varchar_pattern_ops"},
    )

    op.create_table(
        "llm_model_profile_versions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("protocol", sa.String(32), nullable=False),
        sa.Column("base_url", sa.String(2048), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("api_key_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("api_key_nonce", sa.LargeBinary(12), nullable=False),
        sa.Column("api_key_key_version", sa.Integer(), nullable=False),
        sa.Column("context_window_tokens", sa.Integer(), nullable=False),
        sa.Column("tokenizer_encoding", sa.String(32), nullable=False),
        sa.Column("supports_stream", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("supports_tools", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "supports_parallel_tool_calls",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("max_output_tokens", sa.Integer(), nullable=False),
        sa.Column("request_timeout_seconds", sa.Integer(), nullable=False),
        sa.Column("execution_timeout_seconds", sa.Integer(), nullable=False),
        sa.Column("max_concurrency", sa.Integer(), nullable=False),
        sa.Column("temperature", sa.Float(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("visible_to_users", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("scenario_label", sa.String(64), nullable=True),
        sa.Column("scenario_description", sa.String(255), nullable=True),
        sa.Column("supports_vision", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("protocol = 'chat_completions'", name="ck_llm_profile_protocol"),
        sa.CheckConstraint("context_window_tokens > 0", name="ck_llm_profile_context_window"),
        sa.CheckConstraint(
            "tokenizer_encoding IN ('cl100k_base', 'o200k_base')",
            name="ck_llm_profile_tokenizer_encoding",
        ),
        sa.CheckConstraint(
            "supports_stream IN (true, false)", name="ck_llm_profile_supports_stream"
        ),
        sa.CheckConstraint("supports_tools IN (true, false)", name="ck_llm_profile_supports_tools"),
        sa.CheckConstraint(
            "supports_parallel_tool_calls IN (true, false)",
            name="ck_llm_profile_supports_parallel_tool_calls",
        ),
        sa.CheckConstraint("max_output_tokens > 0", name="ck_llm_profile_output_tokens"),
        sa.CheckConstraint("request_timeout_seconds > 0", name="ck_llm_profile_request_timeout"),
        sa.CheckConstraint(
            "execution_timeout_seconds > 0", name="ck_llm_profile_execution_timeout"
        ),
        sa.CheckConstraint("max_concurrency > 0", name="ck_llm_profile_concurrency"),
        sa.CheckConstraint("version >= 1", name="ck_llm_profile_version"),
    )

    op.create_table(
        "resource_visibility_grants",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("resource_type", sa.String(32), nullable=False),
        sa.Column("resource_id", sa.String(128), nullable=False),
        sa.Column("target_type", sa.String(16), nullable=False),
        sa.Column("target_id", sa.String(64), nullable=False),
        sa.Column("include_subdepartments", sa.Boolean(), nullable=False, server_default=sa.true()),
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

    op.create_table(
        "system_prompt_versions",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_by", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("restored_from_version_id", sa.String(128), nullable=True),
        sa.CheckConstraint(
            "kind IN ('main', 'compression')",
            name="ck_system_prompt_versions_kind",
        ),
        sa.UniqueConstraint("id", "kind", name="uq_system_prompt_versions_id_kind"),
        sa.ForeignKeyConstraint(
            ["restored_from_version_id", "kind"],
            ["system_prompt_versions.id", "system_prompt_versions.kind"],
            name="fk_system_prompt_versions_restored_same_kind",
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "ix_system_prompt_versions_kind_created_id",
        "system_prompt_versions",
        ["kind", "created_at", "id"],
    )

    op.create_table(
        "users",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("email", sa.String(320), nullable=True),
        sa.Column("password_hash", sa.String(255), nullable=True),
        sa.Column("display_name", sa.String(80), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("role", sa.String(16), nullable=False, server_default="member"),
        sa.Column("auth_provider", sa.String(16), nullable=False, server_default="local"),
        sa.Column("external_union_id", sa.String(128), nullable=True),
        sa.Column("external_open_id", sa.String(128), nullable=True),
        sa.Column("employee_no", sa.String(64), nullable=True),
        sa.Column("avatar_url", sa.String(1024), nullable=True),
        sa.Column("job_title", sa.String(120), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_time", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('active', 'disabled')", name="ck_users_status"),
        sa.CheckConstraint("role IN ('super_admin', 'dept_admin', 'member')", name="ck_users_role"),
        sa.CheckConstraint(
            "email IS NULL OR email = lower(email)", name="ck_users_email_normalized"
        ),
        sa.CheckConstraint(
            "auth_provider IN ('local', 'feishu', 'mock')", name="ck_users_auth_provider"
        ),
        sa.CheckConstraint(
            "(auth_provider != 'local') OR (password_hash IS NOT NULL)",
            name="ck_users_local_password",
        ),
    )
    op.create_index(
        "uq_users_email_active",
        "users",
        ["email"],
        unique=True,
        postgresql_where=sa.text("email IS NOT NULL AND deleted_time IS NULL"),
    )
    op.create_index(
        "uq_users_external_union_id",
        "users",
        ["external_union_id"],
        unique=True,
        postgresql_where=sa.text("external_union_id IS NOT NULL AND deleted_time IS NULL"),
    )
    op.create_index(
        "uq_users_external_open_id",
        "users",
        ["external_open_id"],
        unique=True,
        postgresql_where=sa.text("external_open_id IS NOT NULL AND deleted_time IS NULL"),
    )

    op.create_table(
        "auth_sessions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(128),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("token_hash", name="uq_auth_sessions_token_hash"),
    )
    op.create_index(
        "ix_auth_sessions_user_state",
        "auth_sessions",
        ["user_id", "revoked_time", "expires_at"],
    )

    op.create_table(
        "dept_admin_scopes",
        sa.Column(
            "user_id",
            sa.String(128),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "department_id",
            sa.String(64),
            sa.ForeignKey("departments.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("include_subdepartments", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "granted_by",
            sa.String(128),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_dept_admin_scopes_dept", "dept_admin_scopes", ["department_id"])

    op.create_table(
        "user_departments",
        sa.Column(
            "user_id",
            sa.String(128),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "department_id",
            sa.String(64),
            sa.ForeignKey("departments.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index(
        "ix_user_departments_department",
        "user_departments",
        ["department_id", "user_id"],
    )

    op.create_table(
        "user_work_profiles",
        sa.Column(
            "user_id",
            sa.String(128),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("custom_instructions", sa.Text(), nullable=False, server_default=""),
        sa.Column("memory_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "user_memories",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(128),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("content", sa.String(500), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("source IN ('manual', 'chat_explicit')", name="ck_user_memories_source"),
    )
    op.create_index(
        "ix_user_memories_user_updated",
        "user_memories",
        ["user_id", sa.text("updated_at DESC"), sa.text("id DESC")],
    )

    # 3. LLM 默认模型、压缩策略及系统提示词当前指针表。
    op.create_table(
        "llm_default_model_profile",
        sa.Column("singleton_id", sa.Integer(), primary_key=True),
        sa.Column(
            "profile_id",
            sa.String(64),
            sa.ForeignKey("llm_model_profile_versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.CheckConstraint("singleton_id = 1", name="ck_llm_default_singleton"),
    )

    op.create_table(
        "llm_compression_policy_versions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("compression_model_profile_id", sa.String(64), nullable=False),
        sa.Column("trigger_tokens", sa.BigInteger(), nullable=False),
        sa.Column("recent_ratio", sa.Float(), nullable=False),
        sa.Column("recent_token_cap", sa.BigInteger(), nullable=False),
        sa.Column("summary_ratio", sa.Float(), nullable=False),
        sa.Column("summary_token_cap", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("trigger_tokens > 0", name="ck_compression_trigger_tokens"),
        sa.CheckConstraint("recent_ratio > 0 AND recent_ratio < 1", name="ck_compression_recent"),
        sa.CheckConstraint("recent_token_cap > 0", name="ck_compression_recent_token_cap"),
        sa.CheckConstraint(
            "summary_ratio > 0 AND summary_ratio < 1", name="ck_compression_summary"
        ),
        sa.CheckConstraint("summary_token_cap > 0", name="ck_compression_summary_token_cap"),
        sa.ForeignKeyConstraint(
            ["compression_model_profile_id"],
            ["llm_model_profile_versions.id"],
            name="fk_compression_policy_model",
            ondelete="RESTRICT",
        ),
    )

    op.create_table(
        "llm_current_compression_policy",
        sa.Column("singleton_id", sa.Integer(), primary_key=True),
        sa.Column("policy_version_id", sa.String(64), nullable=False),
        sa.CheckConstraint("singleton_id = 1", name="ck_current_compression_policy_singleton"),
        sa.ForeignKeyConstraint(
            ["policy_version_id"],
            ["llm_compression_policy_versions.id"],
            name="fk_current_compression_policy_version",
            ondelete="RESTRICT",
        ),
    )

    op.create_table(
        "system_prompt_settings",
        sa.Column("kind", sa.String(16), primary_key=True),
        sa.Column("current_version_id", sa.String(128), nullable=False),
        sa.CheckConstraint(
            "kind IN ('main', 'compression')",
            name="ck_system_prompt_settings_kind",
        ),
        sa.ForeignKeyConstraint(
            ["current_version_id", "kind"],
            ["system_prompt_versions.id", "system_prompt_versions.kind"],
            name="fk_system_prompt_settings_current_same_kind",
            ondelete="RESTRICT",
        ),
    )

    # 4. MCP、Subagent、Skill 资产表。
    op.create_table(
        "mcp_servers",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("transport", sa.String(32), nullable=False),
        sa.Column("config_json", postgresql.JSONB(), nullable=False),
        sa.Column("secrets_ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column("secrets_nonce", sa.LargeBinary(), nullable=True),
        sa.Column("key_version", sa.Integer(), nullable=True),
        sa.Column("config_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("visibility_scope", sa.String(16), nullable=False, server_default="company"),
        sa.Column(
            "owner_department_id",
            sa.String(64),
            sa.ForeignKey("departments.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "created_by",
            sa.String(128),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("tools_discovered_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "transport IN ('streamable_http', 'stdio')",
            name="ck_mcp_servers_transport",
        ),
        sa.CheckConstraint("config_version >= 1", name="ck_mcp_servers_config_version"),
        sa.CheckConstraint(
            "key_version IS NULL OR key_version >= 1", name="ck_mcp_servers_key_version"
        ),
        sa.CheckConstraint(
            "(secrets_ciphertext IS NULL AND secrets_nonce IS NULL AND key_version IS NULL) "
            "OR (secrets_ciphertext IS NOT NULL AND secrets_nonce IS NOT NULL "
            "AND key_version IS NOT NULL)",
            name="ck_mcp_servers_secret_bundle",
        ),
        sa.CheckConstraint(
            "visibility_scope IN ('company', 'department')",
            name="ck_mcp_servers_visibility_scope",
        ),
        sa.CheckConstraint(
            "(visibility_scope = 'company' AND owner_department_id IS NULL) "
            "OR (visibility_scope = 'department' AND owner_department_id IS NOT NULL)",
            name="ck_mcp_servers_owner_department_scope",
        ),
    )
    op.create_index(
        "uq_mcp_servers_name_active",
        "mcp_servers",
        ["name"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "ix_mcp_servers_visibility_dept",
        "mcp_servers",
        ["visibility_scope", "owner_department_id"],
    )

    op.create_table(
        "mcp_tools",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "server_id",
            sa.String(64),
            sa.ForeignKey("mcp_servers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("remote_name", sa.String(256), nullable=False),
        sa.Column("model_name", sa.String(128), nullable=False),
        sa.Column("description", sa.String(8192), nullable=False),
        sa.Column("input_schema_json", postgresql.JSONB(), nullable=False),
        sa.Column(
            "requires_approval",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("display_name", sa.String(120), nullable=True),
        sa.Column("approval_summary_template", sa.String(500), nullable=True),
        sa.Column(
            "parameter_labels_json",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("server_id", "remote_name", name="uq_mcp_tools_server_remote"),
        sa.UniqueConstraint("model_name", name="uq_mcp_tools_model_name"),
    )

    op.create_table(
        "subagents",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("slug", sa.String(64), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("description", sa.String(1000), nullable=False),
        sa.Column("system_prompt", sa.Text(), nullable=False),
        sa.Column("model_profile_id", sa.String(64), nullable=True),
        sa.Column(
            "allowed_tool_ids",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("visibility_scope", sa.String(16), nullable=False, server_default="company"),
        sa.Column(
            "owner_department_id",
            sa.String(64),
            sa.ForeignKey("departments.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("max_llm_requests", sa.Integer(), nullable=True),
        sa.Column("timeout_seconds", sa.Integer(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("created_by", sa.String(128), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("version >= 1", name="ck_subagents_version"),
        sa.CheckConstraint(
            "max_llm_requests IS NULL OR (max_llm_requests >= 1 AND max_llm_requests <= 50)",
            name="ck_subagents_max_llm_requests",
        ),
        sa.CheckConstraint(
            "timeout_seconds IS NULL OR (timeout_seconds >= 10 AND timeout_seconds <= 1800)",
            name="ck_subagents_timeout_seconds",
        ),
        sa.CheckConstraint(
            "visibility_scope IN ('company', 'department')",
            name="ck_subagents_visibility_scope",
        ),
        sa.CheckConstraint(
            "(visibility_scope = 'company' AND owner_department_id IS NULL) "
            "OR (visibility_scope = 'department' AND owner_department_id IS NOT NULL)",
            name="ck_subagents_owner_department_scope",
        ),
    )
    op.create_index(
        "uq_subagents_slug_active",
        "subagents",
        ["slug"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "ix_subagents_enabled_created",
        "subagents",
        ["enabled", "created_at", "id"],
    )
    op.create_index(
        "ix_subagents_visibility_dept",
        "subagents",
        ["visibility_scope", "owner_department_id", "deleted_at"],
    )

    # skills 与 skill_versions 存在双向外键，
    # 先建两表后再通过 ALTER TABLE 补建 current_version 外键。
    op.create_table(
        "skills",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("slug", sa.String(64), nullable=False),
        sa.Column("scope", sa.String(16), nullable=False, server_default="company"),
        sa.Column("owner_id", sa.String(128), nullable=True),
        sa.Column("owner_department_id", sa.String(64), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("current_version_id", sa.String(128), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("script_requires_approval", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("format", sa.String(16), nullable=False, server_default=sa.text("'package'")),
        sa.Column("featured", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("starter_prompt", sa.String(500), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("created_by", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("revision >= 1", name="ck_skills_revision"),
        sa.CheckConstraint(
            "scope IN ('company', 'department', 'personal')",
            name="ck_skills_scope",
        ),
        sa.CheckConstraint(
            "(scope = 'company' AND owner_id IS NULL AND owner_department_id IS NULL "
            "AND deleted_at IS NULL) OR "
            "(scope = 'department' AND owner_id IS NULL AND owner_department_id IS NOT NULL) OR "
            "(scope = 'personal' AND owner_id IS NOT NULL AND owner_department_id IS NULL)",
            name="ck_skills_owner_scope",
        ),
        sa.CheckConstraint("format IN ('markdown', 'package')", name="ck_skills_format"),
        sa.CheckConstraint(
            "(scope != 'personal') OR (format = 'markdown' AND NOT featured)",
            name="ck_skills_personal_format_and_featured",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["users.id"],
            name="fk_skills_owner",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["owner_department_id"],
            ["departments.id"],
            name="fk_skills_owner_department",
            ondelete="RESTRICT",
        ),
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
        "uq_skills_personal_owner_slug",
        "skills",
        ["owner_id", "slug"],
        unique=True,
        postgresql_where=sa.text("scope = 'personal' AND deleted_at IS NULL"),
    )
    op.create_index("ix_skills_public_order", "skills", ["enabled", "slug", "id"])
    op.create_index(
        "ix_skills_owner_scope_enabled",
        "skills",
        ["owner_id", "scope", "enabled", "deleted_at"],
    )
    op.create_index(
        "ix_skills_department_scope",
        "skills",
        ["owner_department_id", "scope", "enabled", "deleted_at"],
    )

    op.create_table(
        "skill_versions",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("skill_id", sa.String(128), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(64), nullable=False),
        sa.Column("guide", sa.Text(), nullable=False),
        sa.Column("manifest", postgresql.JSONB(), nullable=False),
        sa.Column("object_key", sa.String(1024), nullable=True),
        sa.Column("package_sha256", sa.String(64), nullable=False),
        sa.Column("dependency_requirements", postgresql.JSONB(), nullable=False),
        sa.Column("dependency_check", postgresql.JSONB(), nullable=False),
        sa.Column("created_by", sa.String(128), nullable=True),
        sa.Column("request_operation", sa.String(16), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("request_target_skill_id", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("ordinal > 0", name="ck_skill_versions_ordinal"),
        sa.CheckConstraint(
            "request_operation IN ('import', 'add_version', 'personal_create', 'personal_publish')",
            name="ck_skill_versions_request_operation",
        ),
        sa.CheckConstraint(
            "(request_operation IN ('import', 'personal_create') "
            "AND request_target_skill_id IS NULL) OR "
            "(request_operation IN ('add_version', 'personal_publish') "
            "AND request_target_skill_id IS NOT NULL)",
            name="ck_skill_versions_request_target",
        ),
        sa.UniqueConstraint("skill_id", "ordinal", name="uq_skill_versions_ordinal"),
        sa.UniqueConstraint("object_key", name="uq_skill_versions_object_key"),
        sa.UniqueConstraint("id", "skill_id", name="uq_skill_versions_id_skill"),
        sa.UniqueConstraint(
            "created_by",
            "request_operation",
            "idempotency_key",
            name="uq_skill_versions_request_key",
        ),
        sa.ForeignKeyConstraint(
            ["skill_id"],
            ["skills.id"],
            name="fk_skill_versions_skill",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name="fk_skill_versions_created_by",
            ondelete="RESTRICT",
        ),
    )
    op.create_index("ix_skill_versions_package_sha256", "skill_versions", ["package_sha256"])
    op.create_foreign_key(
        "fk_skills_current_version",
        "skills",
        "skill_versions",
        ["current_version_id", "id"],
        ["id", "skill_id"],
        ondelete="RESTRICT",
    )

    # 5. 项目与知识库表。
    op.create_table(
        "projects",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "owner_id",
            sa.String(128),
            sa.ForeignKey("users.id", name="fk_projects_owner_id_users"),
            nullable=False,
        ),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("description", sa.String(500), nullable=True),
        sa.Column("instructions", sa.Text(), nullable=True),
        sa.Column(
            "default_skill_ids",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_time", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("id", "owner_id", name="uq_projects_id_owner"),
    )
    op.create_index(
        "ix_projects_owner_active_updated",
        "projects",
        ["owner_id", sa.text("updated_at DESC"), sa.text("id DESC")],
        postgresql_where=sa.text("deleted_time IS NULL"),
    )

    op.create_table(
        "knowledge_bases",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "owner_id",
            sa.String(128),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("description", sa.String(500), nullable=True),
        sa.Column("visibility_scope", sa.String(16), nullable=False, server_default="personal"),
        sa.Column(
            "owner_department_id",
            sa.String(64),
            sa.ForeignKey("departments.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_time", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "visibility_scope IN ('company', 'department', 'personal')",
            name="ck_knowledge_bases_visibility_scope",
        ),
        sa.CheckConstraint(
            "(visibility_scope = 'department' AND owner_department_id IS NOT NULL) "
            "OR (visibility_scope IN ('company', 'personal') AND owner_department_id IS NULL)",
            name="ck_knowledge_bases_owner_department_scope",
        ),
    )
    op.create_index(
        "ix_knowledge_bases_owner_updated",
        "knowledge_bases",
        ["owner_id", "updated_at"],
    )
    op.create_index(
        "ix_knowledge_bases_visibility_dept",
        "knowledge_bases",
        ["visibility_scope", "owner_department_id", "deleted_time"],
    )

    op.create_table(
        "knowledge_sources",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "owner_id",
            sa.String(128),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("storage_key", sa.String(256), nullable=False, unique=True),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("media_type", sa.String(128), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_knowledge_sources_owner_id", "knowledge_sources", ["owner_id"])

    op.create_table(
        "knowledge_documents",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("owner_id", sa.String(128), nullable=False),
        sa.Column("knowledge_base_id", sa.String(64), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("active_version_id", sa.String(64), nullable=True),
        sa.Column("pending_version_id", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_time", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["knowledge_base_id"],
            ["knowledge_bases.id"],
            name="fk_knowledge_documents_base",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_knowledge_documents_owner_kb",
        "knowledge_documents",
        ["owner_id", "knowledge_base_id", "updated_at"],
    )
    op.create_index(
        "ix_knowledge_documents_kb_active_updated",
        "knowledge_documents",
        ["knowledge_base_id", sa.text("updated_at DESC"), sa.text("id DESC")],
        postgresql_where=sa.text("deleted_time IS NULL"),
    )

    op.create_table(
        "knowledge_document_versions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("document_id", sa.String(64), nullable=False),
        sa.Column("owner_id", sa.String(128), nullable=False),
        sa.Column("source_id", sa.String(64), nullable=True),
        sa.Column("source_text", sa.Text(), nullable=True),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("profile_id", sa.String(128), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("warnings", postgresql.JSONB(), nullable=False),
        sa.Column("chunk_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("id", "document_id", name="uq_knowledge_versions_id_document"),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["knowledge_documents.id"],
            name="fk_knowledge_versions_document",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["knowledge_sources.id"],
            name="fk_knowledge_versions_source",
        ),
        sa.CheckConstraint(
            "(source_id IS NULL) != (source_text IS NULL)",
            name="ck_knowledge_version_source",
        ),
        sa.CheckConstraint(
            "state IN ('queued','processing','ready','failed')",
            name="ck_knowledge_version_state",
        ),
    )
    op.create_index(
        "ix_knowledge_versions_doc_created",
        "knowledge_document_versions",
        ["document_id", sa.text("created_at DESC"), sa.text("id DESC")],
    )
    op.create_index(
        "ix_knowledge_versions_source_id",
        "knowledge_document_versions",
        ["source_id"],
        postgresql_where=sa.text("source_id IS NOT NULL"),
    )

    op.create_table(
        "knowledge_chunks",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("document_id", sa.String(64), nullable=False),
        sa.Column("version_id", sa.String(64), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("locator", postgresql.JSONB(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("search_document", postgresql.TSVECTOR(), nullable=False),
        sa.Column("embedding", Vector(1024), nullable=True),
        sa.UniqueConstraint("version_id", "ordinal", name="uq_knowledge_chunks_version_ordinal"),
        sa.ForeignKeyConstraint(
            ["version_id", "document_id"],
            ["knowledge_document_versions.id", "knowledge_document_versions.document_id"],
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_knowledge_chunks_fts",
        "knowledge_chunks",
        ["search_document"],
        postgresql_using="gin",
    )
    op.create_index(
        "ix_knowledge_chunks_embedding_hnsw",
        "knowledge_chunks",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
        postgresql_where=sa.text("embedding IS NOT NULL"),
    )

    op.create_table(
        "knowledge_jobs",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "owner_id",
            sa.String(128),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("target_id", sa.String(64), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fencing_token", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "state IN ('queued','processing','retry','done','failed')",
            name="ck_knowledge_jobs_state",
        ),
    )
    op.create_index("ix_knowledge_jobs_ready", "knowledge_jobs", ["state", "next_attempt_at"])
    op.create_index("ix_knowledge_jobs_lease", "knowledge_jobs", ["state", "lease_until"])

    op.create_table(
        "knowledge_requests",
        sa.Column(
            "owner_id",
            sa.String(128),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("operation", sa.String(16), primary_key=True),
        sa.Column("request_key", sa.String(128), primary_key=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("document_id", sa.String(64), nullable=False),
        sa.Column("version_id", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "project_knowledge_bases",
        sa.Column("project_id", sa.String(64), primary_key=True),
        sa.Column("knowledge_base_id", sa.String(64), primary_key=True),
        sa.Column("owner_id", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id", "owner_id"],
            ["projects.id", "projects.owner_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["knowledge_base_id"],
            ["knowledge_bases.id"],
            name="fk_project_knowledge_bases_base",
            ondelete="CASCADE",
        ),
    )

    # 6. 会话、消息流、Run 编排与工具执行审计表。
    op.create_table(
        "sessions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "owner_id",
            sa.String(128),
            sa.ForeignKey("users.id", name="fk_sessions_owner_id_users"),
            nullable=False,
        ),
        sa.Column("title", sa.String(120), nullable=True),
        sa.Column("project_id", sa.String(64), nullable=True),
        sa.Column("last_message_sequence", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_time", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("id", "owner_id", name="uq_sessions_id_owner"),
        sa.CheckConstraint(
            "last_message_sequence >= 0",
            name="ck_sessions_last_message_sequence",
        ),
        sa.CheckConstraint(
            "title IS NULL OR length(title) <= 120",
            name="ck_sessions_title_length",
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "owner_id"],
            ["projects.id", "projects.owner_id"],
            name="fk_sessions_project_owner_projects",
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "ix_sessions_owner_active_updated",
        "sessions",
        ["owner_id", sa.text("updated_at DESC"), sa.text("id DESC")],
        postgresql_where=sa.text("deleted_time IS NULL"),
    )
    op.create_index(
        "ix_sessions_owner_project_active_updated",
        "sessions",
        ["owner_id", "project_id", sa.text("updated_at DESC"), sa.text("id DESC")],
        postgresql_where=sa.text("deleted_time IS NULL"),
    )

    # conversation_messages、execution_messages 与 runs 存在双向延迟外键；
    # 先建消息表与 runs 表，随后再为消息表添加 DEFERRABLE INITIALLY DEFERRED 复合外键。
    op.create_table(
        "conversation_messages",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "session_id",
            sa.String(64),
            sa.ForeignKey("sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("parts", postgresql.JSONB(), nullable=False),
        sa.Column(
            "reply_to_message_id",
            sa.String(64),
            sa.ForeignKey("conversation_messages.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("run_id", sa.String(128), nullable=False),
        sa.Column("context_turn_id", sa.String(64), nullable=False),
        sa.Column("client_message_id", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata", postgresql.JSONB(), nullable=False),
        sa.CheckConstraint(
            "role IN ('user', 'assistant')",
            name="ck_conversation_messages_role",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'streaming', 'completed', 'failed', 'cancelled')",
            name="ck_conversation_messages_status",
        ),
        sa.UniqueConstraint(
            "session_id",
            "sequence",
            name="uq_conversation_messages_session_sequence",
        ),
        sa.UniqueConstraint(
            "session_id",
            "client_message_id",
            name="uq_conversation_messages_client",
        ),
        sa.UniqueConstraint(
            "id",
            "session_id",
            name="uq_conversation_messages_id_session",
        ),
    )
    op.create_index(
        "ix_conversation_messages_session_role_sequence",
        "conversation_messages",
        ["session_id", "role", "sequence"],
    )
    op.create_index(
        "ix_conversation_messages_session_created",
        "conversation_messages",
        ["session_id", "created_at", "id"],
    )
    op.create_index(
        "ix_conversation_messages_session_run_seq",
        "conversation_messages",
        ["session_id", "run_id", "sequence"],
    )

    op.create_table(
        "execution_messages",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "session_id",
            sa.String(64),
            sa.ForeignKey("sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("run_id", sa.String(128), nullable=False),
        sa.Column("thread_id", sa.String(128), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("parts", postgresql.JSONB(), nullable=False),
        sa.Column("tool_call_id", sa.String(128), nullable=True),
        sa.Column(
            "source_message_id",
            sa.String(64),
            sa.ForeignKey("conversation_messages.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("metadata", postgresql.JSONB(), nullable=False),
        sa.CheckConstraint(
            "role IN ('system', 'user', 'assistant', 'tool')",
            name="ck_execution_messages_role",
        ),
        sa.CheckConstraint(
            "(role = 'tool' AND tool_call_id IS NOT NULL) "
            "OR (role <> 'tool' AND tool_call_id IS NULL)",
            name="ck_execution_messages_tool_call",
        ),
        sa.UniqueConstraint(
            "session_id",
            "sequence",
            name="uq_execution_messages_session_sequence",
        ),
        sa.UniqueConstraint(
            "id",
            "session_id",
            name="uq_execution_messages_id_session",
        ),
        sa.UniqueConstraint(
            "session_id",
            "run_id",
            "thread_id",
            "tool_call_id",
            name="uq_execution_messages_scope_tool",
        ),
    )
    op.create_index(
        "ix_execution_messages_thread_sequence",
        "execution_messages",
        ["thread_id", "sequence"],
    )
    op.create_index(
        "ix_execution_messages_run_sequence",
        "execution_messages",
        ["run_id", "sequence"],
    )

    op.create_table(
        "run_requests",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("owner_id", sa.String(128), nullable=False),
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column("operation", sa.String(32), nullable=False),
        sa.Column("request_id", sa.String(128), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("result", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "operation IN ('submit', 'answer', 'retry', 'decide_tool_approval')",
            name="ck_run_requests_op",
        ),
        sa.UniqueConstraint(
            "owner_id",
            "session_id",
            "operation",
            "request_id",
            name="uq_run_requests_scope_key",
        ),
        sa.ForeignKeyConstraint(
            ["session_id", "owner_id"],
            ["sessions.id", "sessions.owner_id"],
            name="fk_run_requests_session_owner",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_run_requests_session_created",
        "run_requests",
        ["session_id", "created_at"],
    )

    op.create_table(
        "runs",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column("owner_id", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("trigger_message_id", sa.String(64), nullable=False),
        sa.Column("context_cutoff_sequence", sa.BigInteger(), nullable=False),
        sa.Column("retry_of_run_id", sa.String(128), nullable=True),
        sa.Column("model_config_version_id", sa.String(64), nullable=False),
        sa.Column("model_snapshot_json", postgresql.JSONB(), nullable=False),
        sa.Column("main_prompt_version_id", sa.String(128), nullable=False),
        sa.Column("compression_prompt_version_id", sa.String(128), nullable=False),
        sa.Column("compression_policy_version_id", sa.String(64), nullable=True),
        sa.Column("skill_snapshot", postgresql.JSONB(), nullable=True),
        sa.Column(
            "input_file_ids",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        sa.Column("knowledge_base_ids", postgresql.JSONB(), nullable=True),
        sa.Column("tool_snapshot", postgresql.JSONB(), nullable=True),
        sa.Column("auth_session_id", sa.String(128), nullable=True),
        sa.Column(
            "llm_request_count", sa.BigInteger(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("prompt_template_version", sa.String(64), nullable=False),
        sa.Column("event_sequence", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("execution_epoch", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("worker_id", sa.String(128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("execution_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("execution_deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("first_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("safe_error_code", sa.String(128), nullable=True),
        sa.Column("report_message_id", sa.String(64), nullable=True),
        sa.Column(
            "usage",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("parent_run_id", sa.String(128), nullable=True),
        sa.Column("root_run_id", sa.String(128), nullable=True),
        sa.Column("parent_tool_call_id", sa.String(256), nullable=True),
        sa.Column("depth", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("subagent_profile_id", sa.String(64), nullable=True),
        sa.Column("subagent_slug", sa.String(64), nullable=True),
        sa.Column("subagent_name", sa.String(100), nullable=True),
        sa.Column("subagent_system_prompt", sa.Text(), nullable=True),
        sa.Column("max_llm_requests", sa.Integer(), nullable=True),
        sa.Column("timeout_seconds", sa.Integer(), nullable=True),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'waiting_for_input', "
            "'waiting_for_approval', 'waiting_for_subagents', 'cancelling', "
            "'completed', 'failed', 'cancelled')",
            name="ck_runs_status",
        ),
        sa.CheckConstraint("context_cutoff_sequence >= 0", name="ck_runs_context_cutoff"),
        sa.CheckConstraint("event_sequence >= 0", name="ck_runs_event_sequence"),
        sa.CheckConstraint("execution_epoch >= 0", name="ck_runs_execution_epoch"),
        sa.CheckConstraint(
            "llm_request_count >= 0 AND llm_request_count <= 50",
            name="ck_runs_llm_request_count",
        ),
        sa.ForeignKeyConstraint(
            ["session_id", "owner_id"],
            ["sessions.id", "sessions.owner_id"],
            name="fk_runs_session_owner",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["trigger_message_id", "session_id"],
            ["conversation_messages.id", "conversation_messages.session_id"],
            name="fk_runs_trigger_message_session",
            deferrable=True,
            initially="DEFERRED",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["report_message_id", "session_id"],
            ["conversation_messages.id", "conversation_messages.session_id"],
            name="fk_runs_report_message_session",
            deferrable=True,
            initially="DEFERRED",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["retry_of_run_id", "session_id"],
            ["runs.id", "runs.session_id"],
            name="fk_runs_retry_session",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("id", "session_id", name="uq_runs_id_session_pair"),
        sa.ForeignKeyConstraint(
            ["model_config_version_id"],
            ["llm_model_profile_versions.id"],
            name="fk_runs_model_profile",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["main_prompt_version_id"],
            ["system_prompt_versions.id"],
            name="fk_runs_main_prompt_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["compression_prompt_version_id"],
            ["system_prompt_versions.id"],
            name="fk_runs_compression_prompt_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["compression_policy_version_id"],
            ["llm_compression_policy_versions.id"],
            name="fk_runs_compression_policy",
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "uq_runs_active_session",
        "runs",
        ["session_id"],
        unique=True,
        postgresql_where=sa.text(
            "depth = 0 AND status IN ('queued', 'running', 'waiting_for_input', "
            "'waiting_for_approval', 'waiting_for_subagents', 'cancelling')"
        ),
    )
    op.create_index("ix_runs_claim", "runs", ["status", "queued_at", "id"])
    op.create_index("ix_runs_lease", "runs", ["status", "lease_expires_at"])
    op.create_index(
        "ix_runs_session_created",
        "runs",
        ["session_id", sa.text("created_at DESC"), sa.text("id DESC")],
    )
    op.create_index("ix_runs_session_status", "runs", ["session_id", "status"])
    op.create_index("ix_runs_parent_run_id", "runs", ["parent_run_id"])
    op.create_index("ix_runs_root_run_id", "runs", ["root_run_id"])

    # 为两张消息表补建指向 runs 的 DEFERRABLE INITIALLY DEFERRED 复合外键。
    op.create_foreign_key(
        "fk_conversation_messages_run_session",
        "conversation_messages",
        "runs",
        ["run_id", "session_id"],
        ["id", "session_id"],
        ondelete="RESTRICT",
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_foreign_key(
        "fk_execution_messages_run_session",
        "execution_messages",
        "runs",
        ["run_id", "session_id"],
        ["id", "session_id"],
        ondelete="RESTRICT",
        deferrable=True,
        initially="DEFERRED",
    )

    op.create_table(
        "run_events",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("run_id", sa.String(128), nullable=False),
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("type", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.UniqueConstraint("run_id", "sequence", name="uq_run_events_run_sequence"),
        sa.ForeignKeyConstraint(
            ["run_id", "session_id"],
            ["runs.id", "runs.session_id"],
            name="fk_run_events_run_session",
            ondelete="CASCADE",
        ),
    )

    op.create_table(
        "run_input_requests",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("run_id", sa.String(128), nullable=False),
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column("question_message_id", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("answer_message_id", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'answered', 'closed')",
            name="ck_run_input_requests_status",
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "session_id"],
            ["runs.id", "runs.session_id"],
            name="fk_run_input_requests_run_session",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["question_message_id", "session_id"],
            ["conversation_messages.id", "conversation_messages.session_id"],
            name="fk_run_input_requests_question_session",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["answer_message_id", "session_id"],
            ["conversation_messages.id", "conversation_messages.session_id"],
            name="fk_run_input_requests_answer_session",
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "uq_run_input_requests_pending",
        "run_input_requests",
        ["run_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "ix_run_input_requests_run_created",
        "run_input_requests",
        ["run_id", "created_at", "id"],
    )

    op.create_table(
        "run_tool_calls",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("run_id", sa.String(128), nullable=False),
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column("tool_call_id", sa.String(256), nullable=False),
        sa.Column("ordinal", sa.BigInteger(), nullable=False),
        sa.Column("tool_id", sa.String(64), nullable=True),
        sa.Column("tool_kind", sa.String(16), nullable=True),
        sa.Column("model_name", sa.String(256), nullable=False),
        sa.Column("execution_epoch", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("call_message_id", sa.String(64), nullable=False),
        sa.Column("result_message_id", sa.String(64), nullable=True),
        sa.Column("requires_approval", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approval_decision", sa.String(8), nullable=True),
        sa.Column("approval_decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("safe_error_code", sa.String(128), nullable=True),
        sa.Column("outcome", postgresql.JSONB(), nullable=True),
        sa.Column("safe_skill_projection", postgresql.JSONB(), nullable=True),
        sa.Column("round", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.CheckConstraint("ordinal > 0", name="ck_run_tool_calls_ordinal"),
        sa.CheckConstraint("execution_epoch > 0", name="ck_run_tool_calls_epoch"),
        sa.CheckConstraint(
            "status IN ('pending_approval', 'approved', 'executing', "
            "'completed', 'failed', 'cancelled', 'unknown')",
            name="ck_run_tool_calls_status",
        ),
        sa.CheckConstraint(
            "tool_kind IS NULL OR tool_kind IN ('builtin', 'mcp')",
            name="ck_run_tool_calls_kind",
        ),
        sa.CheckConstraint(
            "approval_decision IS NULL OR approval_decision IN ('approve', 'reject')",
            name="ck_run_tool_calls_approval_decision",
        ),
        sa.UniqueConstraint("run_id", "tool_call_id", name="uq_run_tool_calls_protocol_id"),
        sa.UniqueConstraint("run_id", "ordinal", name="uq_run_tool_calls_ordinal"),
        sa.UniqueConstraint("result_message_id", name="uq_run_tool_calls_result_message"),
        sa.ForeignKeyConstraint(
            ["run_id", "session_id"],
            ["runs.id", "runs.session_id"],
            name="fk_run_tool_calls_run_session",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["call_message_id", "session_id"],
            ["execution_messages.id", "execution_messages.session_id"],
            name="fk_run_tool_calls_call_message_session",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["result_message_id", "session_id"],
            ["execution_messages.id", "execution_messages.session_id"],
            name="fk_run_tool_calls_result_message_session",
            ondelete="RESTRICT",
        ),
    )
    op.create_index("ix_run_tool_calls_call_message", "run_tool_calls", ["call_message_id"])
    op.create_index("ix_run_tool_calls_run_status", "run_tool_calls", ["run_id", "status"])

    op.create_table(
        "session_context_summaries",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column("owner_id", sa.String(128), nullable=False),
        sa.Column("covered_through_sequence", sa.BigInteger(), nullable=False),
        sa.Column("parent_summary_id", sa.String(128), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("run_id", sa.String(128), nullable=False),
        sa.Column("execution_epoch", sa.BigInteger(), nullable=False),
        sa.Column("operation_id", sa.String(128), nullable=False),
        sa.Column("policy_version_id", sa.String(64), nullable=False),
        sa.Column("compression_model_version_id", sa.String(64), nullable=False),
        sa.Column("compression_prompt_version_id", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("covered_through_sequence > 0", name="ck_context_summary_coverage"),
        sa.CheckConstraint("execution_epoch > 0", name="ck_context_summary_epoch"),
        sa.UniqueConstraint("id", "session_id", name="uq_context_summaries_id_session"),
        sa.UniqueConstraint("run_id", "operation_id", name="uq_context_summary_run_operation"),
        sa.ForeignKeyConstraint(
            ["session_id", "owner_id"],
            ["sessions.id", "sessions.owner_id"],
            name="fk_context_summaries_session_owner",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["parent_summary_id", "session_id"],
            ["session_context_summaries.id", "session_context_summaries.session_id"],
            name="fk_context_summaries_parent_session",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "session_id"],
            ["runs.id", "runs.session_id"],
            name="fk_context_summaries_run_session",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["policy_version_id"],
            ["llm_compression_policy_versions.id"],
            name="fk_context_summaries_policy",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["compression_model_version_id"],
            ["llm_model_profile_versions.id"],
            name="fk_context_summaries_model",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["compression_prompt_version_id"],
            ["system_prompt_versions.id"],
            name="fk_context_summaries_compression_prompt",
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "ix_context_summaries_session_coverage",
        "session_context_summaries",
        ["session_id", "owner_id", "covered_through_sequence", "created_at"],
    )

    op.create_table(
        "session_files",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column("owner_id", sa.String(128), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("label", sa.String(255), nullable=True),
        sa.Column("mime_type", sa.String(255), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("object_key", sa.String(1024), nullable=False),
        sa.Column("source_call_id", sa.String(128), nullable=True),
        sa.Column("output_index", sa.Integer(), nullable=True),
        sa.Column("idempotency_key", sa.String(128), nullable=True),
        sa.Column("request_fingerprint", sa.String(64), nullable=True),
        sa.Column("extraction_status", sa.String(16), nullable=False, server_default="none"),
        sa.Column("extracted_text_key", sa.String(1024), nullable=True),
        sa.Column("extracted_text_bytes", sa.BigInteger(), nullable=True),
        sa.Column("extraction_error_code", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("source IN ('upload', 'artifact')", name="ck_session_files_source"),
        sa.CheckConstraint("size_bytes >= 0", name="ck_session_files_size"),
        sa.CheckConstraint(
            "(source = 'upload' AND source_call_id IS NULL AND output_index IS NULL) OR "
            "(source = 'artifact' AND source_call_id IS NOT NULL AND output_index >= 0)",
            name="ck_session_files_origin",
        ),
        sa.CheckConstraint(
            "(source = 'upload' AND idempotency_key IS NOT NULL "
            "AND request_fingerprint IS NOT NULL) OR "
            "(source = 'artifact' AND idempotency_key IS NULL "
            "AND request_fingerprint IS NULL)",
            name="ck_session_files_idempotency",
        ),
        sa.CheckConstraint(
            "extraction_status IN ('none', 'ready', 'failed')",
            name="ck_session_files_extraction_status",
        ),
        sa.CheckConstraint(
            "extracted_text_bytes IS NULL OR extracted_text_bytes >= 0",
            name="ck_session_files_extracted_text_bytes",
        ),
        sa.CheckConstraint(
            "(extraction_status = 'none' AND extracted_text_key IS NULL "
            "AND extracted_text_bytes IS NULL AND extraction_error_code IS NULL) OR "
            "(extraction_status = 'ready' AND extracted_text_key IS NOT NULL "
            "AND extracted_text_bytes IS NOT NULL AND extraction_error_code IS NULL) OR "
            "(extraction_status = 'failed' AND extracted_text_key IS NULL "
            "AND extracted_text_bytes IS NULL AND extraction_error_code IS NOT NULL)",
            name="ck_session_files_extraction_consistency",
        ),
        sa.UniqueConstraint("session_id", "id", name="uq_session_files_session_id"),
        sa.UniqueConstraint("object_key", name="uq_session_files_object_key"),
        sa.UniqueConstraint(
            "source_call_id",
            "output_index",
            name="uq_session_files_artifact_origin",
        ),
        sa.UniqueConstraint(
            "session_id",
            "owner_id",
            "source",
            "idempotency_key",
            name="uq_session_files_upload_request",
        ),
        sa.ForeignKeyConstraint(
            ["session_id", "owner_id"],
            ["sessions.id", "sessions.owner_id"],
            name="fk_session_files_session_owner",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_call_id"],
            ["run_tool_calls.id"],
            name="fk_session_files_source_call",
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "ix_session_files_session_created",
        "session_files",
        ["session_id", "created_at", "id"],
    )
    op.create_index(
        "uq_session_files_extracted_text_key",
        "session_files",
        ["extracted_text_key"],
        unique=True,
    )

    op.create_table(
        "skill_executions",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("run_id", sa.String(128), nullable=False),
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column("tool_call_record_id", sa.String(128), nullable=False),
        sa.Column("skill_id", sa.String(128), nullable=False),
        sa.Column("skill_version_id", sa.String(128), nullable=False),
        sa.Column("script_path", sa.String(512), nullable=False),
        sa.Column("input_file_ids", postgresql.JSONB(), nullable=False),
        sa.Column("parameters_sha256", sa.String(64), nullable=False),
        sa.Column("python_version", sa.String(64), nullable=False),
        sa.Column("dependency_check", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("log_object_key", sa.String(1024), nullable=True),
        sa.Column("log_truncated", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("log_error_code", sa.String(128), nullable=True),
        sa.Column("error_code", sa.String(128), nullable=True),
        sa.Column("cleanup_error_code", sa.String(128), nullable=True),
        sa.Column("result_artifacts", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'failed', 'unknown')",
            name="ck_skill_executions_status",
        ),
        sa.UniqueConstraint("tool_call_record_id", name="uq_skill_executions_tool_call"),
        sa.ForeignKeyConstraint(
            ["run_id", "session_id"],
            ["runs.id", "runs.session_id"],
            name="fk_skill_executions_run_session",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tool_call_record_id"],
            ["run_tool_calls.id"],
            name="fk_skill_executions_tool_call_record",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["skill_id"],
            ["skills.id"],
            name="fk_skill_executions_skill",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["skill_version_id"],
            ["skill_versions.id"],
            name="fk_skill_executions_version",
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "ix_skill_executions_log_object_key",
        "skill_executions",
        ["log_object_key"],
        postgresql_where=sa.text("log_object_key IS NOT NULL"),
    )

    # 7. 写入主提示词与压缩提示词的初始版本与当前指针种子记录（同时兼容在线执行与 --sql 离线导出）。
    prompt_versions = sa.table(
        "system_prompt_versions",
        sa.column("id", sa.String),
        sa.column("kind", sa.String),
        sa.column("content", sa.Text),
        sa.column("created_by", sa.String),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("restored_from_version_id", sa.String),
    )
    op.execute(
        sa.insert(prompt_versions).values(
            [
                {
                    "id": INITIAL_SYSTEM_PROMPT_IDS["main"],
                    "kind": "main",
                    "content": INITIAL_MAIN_SYSTEM_PROMPT,
                    "created_by": None,
                    "created_at": sa.func.now(),
                    "restored_from_version_id": None,
                },
                {
                    "id": INITIAL_SYSTEM_PROMPT_IDS["compression"],
                    "kind": "compression",
                    "content": INITIAL_COMPRESSION_SYSTEM_PROMPT,
                    "created_by": None,
                    "created_at": sa.func.now(),
                    "restored_from_version_id": None,
                },
            ]
        )
    )
    prompt_settings = sa.table(
        "system_prompt_settings",
        sa.column("kind", sa.String),
        sa.column("current_version_id", sa.String),
    )
    op.execute(
        sa.insert(prompt_settings).values(
            [
                {
                    "kind": "main",
                    "current_version_id": INITIAL_SYSTEM_PROMPT_IDS["main"],
                },
                {
                    "kind": "compression",
                    "current_version_id": INITIAL_SYSTEM_PROMPT_IDS["compression"],
                },
            ]
        )
    )


def downgrade() -> None:
    """按逆依赖顺序拆除双向外键并删除全部 39 张基线表（保留共享的 pgvector 扩展）。"""
    # 1. 先移除指向 runs / skill_versions 的循环外键，避免表删除时触发外键依赖报错。
    op.drop_constraint(
        "fk_execution_messages_run_session",
        "execution_messages",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_conversation_messages_run_session",
        "conversation_messages",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_skills_current_version",
        "skills",
        type_="foreignkey",
    )

    # 2. 按逆拓扑顺序删除全部业务表（PostgreSQL 自动级联删除各表所属索引与局部约束）。
    op.drop_table("skill_executions")
    op.drop_table("session_files")
    op.drop_table("session_context_summaries")
    op.drop_table("run_tool_calls")
    op.drop_table("run_input_requests")
    op.drop_table("run_events")
    op.drop_table("runs")
    op.drop_table("run_requests")
    op.drop_table("execution_messages")
    op.drop_table("conversation_messages")
    op.drop_table("sessions")
    op.drop_table("project_knowledge_bases")
    op.drop_table("knowledge_requests")
    op.drop_table("knowledge_jobs")
    op.drop_table("knowledge_chunks")
    op.drop_table("knowledge_document_versions")
    op.drop_table("knowledge_documents")
    op.drop_table("knowledge_sources")
    op.drop_table("knowledge_bases")
    op.drop_table("projects")
    op.drop_table("skill_versions")
    op.drop_table("skills")
    op.drop_table("subagents")
    op.drop_table("mcp_tools")
    op.drop_table("mcp_servers")
    op.drop_table("system_prompt_settings")
    op.drop_table("llm_current_compression_policy")
    op.drop_table("llm_compression_policy_versions")
    op.drop_table("llm_default_model_profile")
    op.drop_table("user_memories")
    op.drop_table("user_work_profiles")
    op.drop_table("user_departments")
    op.drop_table("dept_admin_scopes")
    op.drop_table("auth_sessions")
    op.drop_table("users")
    op.drop_table("system_prompt_versions")
    op.drop_table("resource_visibility_grants")
    op.drop_table("llm_model_profile_versions")
    op.drop_table("departments")
