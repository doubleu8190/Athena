"""Create administrator Skills, immutable versions, session files, and execution audit."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261117_0018"
down_revision: str | None = "20261116_0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _json_type() -> sa.types.TypeEngine[Any]:
    """PostgreSQL 使用 JSONB，SQLite 迁移测试使用 JSON。"""
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    """新增 Skill/文件目录与 Run 固定快照，旧 Run 明确保留为空快照。"""
    op.create_table(
        "skills",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("slug", sa.String(64), nullable=False),
        sa.Column("current_version_id", sa.String(128)),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column(
            "script_requires_approval",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_by", sa.String(128)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("revision >= 1", name="ck_skills_revision"),
        sa.UniqueConstraint("slug", name="uq_skills_slug"),
    )
    op.create_table(
        "skill_versions",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("skill_id", sa.String(128), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(64), nullable=False),
        sa.Column("guide", sa.Text(), nullable=False),
        sa.Column("manifest", _json_type(), nullable=False),
        sa.Column("object_key", sa.String(1024), nullable=False),
        sa.Column("package_sha256", sa.String(64), nullable=False),
        sa.Column("dependency_requirements", _json_type(), nullable=False),
        sa.Column("dependency_check", _json_type(), nullable=False),
        sa.Column("created_by", sa.String(128)),
        sa.Column("request_operation", sa.String(16), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("request_target_skill_id", sa.String(128)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("ordinal > 0", name="ck_skill_versions_ordinal"),
        sa.CheckConstraint(
            "request_operation IN ('import', 'add_version')",
            name="ck_skill_versions_request_operation",
        ),
        sa.CheckConstraint(
            "(request_operation = 'import' AND request_target_skill_id IS NULL) OR "
            "(request_operation = 'add_version' AND request_target_skill_id IS NOT NULL)",
            name="ck_skill_versions_request_target",
        ),
        sa.UniqueConstraint("skill_id", "ordinal", name="uq_skill_versions_ordinal"),
        sa.UniqueConstraint("object_key", name="uq_skill_versions_object_key"),
        sa.UniqueConstraint("id", "skill_id", name="uq_skill_versions_id_skill"),
        sa.UniqueConstraint("package_sha256", name="uq_skill_versions_package_hash"),
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
    op.create_foreign_key(
        "fk_skills_current_version",
        "skills",
        "skill_versions",
        ["current_version_id", "id"],
        ["id", "skill_id"],
        ondelete="RESTRICT",
        use_alter=True,
    )
    op.create_index(
        "ix_skill_versions_skill_ordinal",
        "skill_versions",
        ["skill_id", "ordinal"],
    )
    op.create_index(
        "ix_skills_public_order",
        "skills",
        ["enabled", "slug", "id"],
    )

    op.create_table(
        "session_files",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column("owner_id", sa.String(128), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("label", sa.String(255)),
        sa.Column("mime_type", sa.String(255), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("object_key", sa.String(1024), nullable=False),
        sa.Column("source_call_id", sa.String(128)),
        sa.Column("output_index", sa.Integer()),
        sa.Column("idempotency_key", sa.String(128)),
        sa.Column("request_fingerprint", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "source IN ('upload', 'artifact')",
            name="ck_session_files_source",
        ),
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

    op.create_table(
        "skill_executions",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("run_id", sa.String(128), nullable=False),
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column("tool_call_record_id", sa.String(128), nullable=False),
        sa.Column("skill_id", sa.String(128), nullable=False),
        sa.Column("skill_version_id", sa.String(128), nullable=False),
        sa.Column("script_path", sa.String(512), nullable=False),
        sa.Column("input_file_ids", _json_type(), nullable=False),
        sa.Column("parameters_sha256", sa.String(64), nullable=False),
        sa.Column("python_version", sa.String(64), nullable=False),
        sa.Column("dependency_check", _json_type(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("log_object_key", sa.String(1024)),
        sa.Column("log_truncated", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("log_error_code", sa.String(128)),
        sa.Column("error_code", sa.String(128)),
        sa.Column("cleanup_error_code", sa.String(128)),
        sa.Column("result_artifacts", _json_type(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'failed', 'unknown')",
            name="ck_skill_executions_status",
        ),
        sa.UniqueConstraint(
            "tool_call_record_id",
            name="uq_skill_executions_tool_call",
        ),
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

    # 旧 Run 没有历史技能证据，迁移后固定为空目录；执行恢复不会补读当前目录。
    op.add_column("runs", sa.Column("skill_snapshot", _json_type()))
    op.add_column(
        "runs",
        sa.Column(
            "input_file_ids",
            _json_type(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
    )


def downgrade() -> None:
    """仅移除本次结构；已有对象按部署清理流程保留，不在此删除外部数据。"""
    op.drop_column("runs", "input_file_ids")
    op.drop_column("runs", "skill_snapshot")
    op.drop_table("skill_executions")
    op.drop_index("ix_session_files_session_created", table_name="session_files")
    op.drop_table("session_files")
    op.drop_index("ix_skills_public_order", table_name="skills")
    op.drop_index("ix_skill_versions_skill_ordinal", table_name="skill_versions")
    op.drop_constraint("fk_skills_current_version", "skills", type_="foreignkey")
    op.drop_table("skill_versions")
    op.drop_table("skills")
