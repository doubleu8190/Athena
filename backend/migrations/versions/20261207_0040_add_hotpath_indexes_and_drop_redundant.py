"""补齐高频热路径缺失索引并清理被唯一约束最左前缀完全覆盖的冗余索引。"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261207_0040"
down_revision: str | None = "20261206_0039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _is_postgresql() -> bool:
    """判断当前迁移目标方言是否为 PostgreSQL，兼容在线连接与离线 SQL 生成。"""
    bind = op.get_bind()
    if bind is not None:
        return bind.dialect.name == "postgresql"
    return op.get_context().dialect.name == "postgresql"


def upgrade() -> None:
    """新增 9 个高频热路径索引，并移除 4 个与唯一约束最左前缀重合的冗余索引。"""
    # 1. 新增高频查询热路径索引，覆盖 Run 列表/活跃检查、消息扫描、知识库目录/版本与原件反查。
    op.create_index(
        "ix_run_input_requests_run_created",
        "run_input_requests",
        ["run_id", "created_at", "id"],
    )
    op.create_index(
        "ix_runs_session_created",
        "runs",
        ["session_id", sa.text("created_at DESC"), sa.text("id DESC")],
    )
    op.create_index(
        "ix_runs_session_status",
        "runs",
        ["session_id", "status"],
    )
    op.create_index(
        "ix_conversation_messages_session_run_seq",
        "conversation_messages",
        ["session_id", "run_id", "sequence"],
    )
    op.create_index(
        "ix_knowledge_documents_kb_active_updated",
        "knowledge_documents",
        ["knowledge_base_id", sa.text("updated_at DESC"), sa.text("id DESC")],
        postgresql_where=sa.text("deleted_time IS NULL"),
        sqlite_where=sa.text("deleted_time IS NULL"),
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
        sqlite_where=sa.text("source_id IS NOT NULL"),
    )
    op.create_index(
        "ix_knowledge_sources_owner_id",
        "knowledge_sources",
        ["owner_id"],
    )
    # 仅在 PostgreSQL 下创建 varchar_pattern_ops 前缀匹配索引，避免 SQLite 方言报错。
    if _is_postgresql():
        op.create_index(
            "ix_departments_path_pattern",
            "departments",
            ["path"],
            postgresql_ops={"path": "varchar_pattern_ops"},
        )

    # 2. 清理已被唯一约束最左前缀完全覆盖的 4 个冗余索引，降低写入维护与缓冲池开销。
    op.drop_index(
        "ix_resource_visibility_grants_resource",
        table_name="resource_visibility_grants",
    )
    op.drop_index(
        "ix_knowledge_chunks_version",
        table_name="knowledge_chunks",
    )
    op.drop_index(
        "ix_run_tool_calls_run_ordinal",
        table_name="run_tool_calls",
    )
    op.drop_index(
        "ix_skill_versions_skill_ordinal",
        table_name="skill_versions",
    )


def downgrade() -> None:
    """对称恢复被清理的 4 个历史索引，并逆序删除本次新增的 9 个索引。"""
    # 1. 重建历史冗余索引，保持降级后与 20261206_0039 结构完全一致。
    op.create_index(
        "ix_skill_versions_skill_ordinal",
        "skill_versions",
        ["skill_id", "ordinal"],
    )
    op.create_index(
        "ix_run_tool_calls_run_ordinal",
        "run_tool_calls",
        ["run_id", "ordinal"],
    )
    op.create_index(
        "ix_knowledge_chunks_version",
        "knowledge_chunks",
        ["version_id"],
    )
    op.create_index(
        "ix_resource_visibility_grants_resource",
        "resource_visibility_grants",
        ["resource_type", "resource_id"],
    )

    # 2. 逆序移除本次新增的 9 个索引。
    if _is_postgresql():
        op.drop_index("ix_departments_path_pattern", table_name="departments")
    op.drop_index("ix_knowledge_sources_owner_id", table_name="knowledge_sources")
    op.drop_index(
        "ix_knowledge_versions_source_id",
        table_name="knowledge_document_versions",
    )
    op.drop_index(
        "ix_knowledge_versions_doc_created",
        table_name="knowledge_document_versions",
    )
    op.drop_index(
        "ix_knowledge_documents_kb_active_updated",
        table_name="knowledge_documents",
    )
    op.drop_index(
        "ix_conversation_messages_session_run_seq",
        table_name="conversation_messages",
    )
    op.drop_index("ix_runs_session_status", table_name="runs")
    op.drop_index("ix_runs_session_created", table_name="runs")
    op.drop_index(
        "ix_run_input_requests_run_created",
        table_name="run_input_requests",
    )
