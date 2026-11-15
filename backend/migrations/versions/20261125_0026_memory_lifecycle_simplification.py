"""简化记忆生命周期协调协议。"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261125_0026"
down_revision: str | None = "20261124_0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _json_type() -> sa.types.TypeEngine[object]:
    """让迁移在 PostgreSQL 使用 JSONB。"""
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    """用用户 generation 和整 Run 提取替换租约、版本与片段协议。"""
    op.add_column(
        "memory_settings",
        sa.Column("generation", sa.Integer(), nullable=False, server_default="1"),
    )
    op.create_check_constraint(
        "ck_memory_settings_generation",
        "memory_settings",
        "generation >= 1",
    )

    # 固定上下文是 Run 自己的读取快照，必须从旧提取状态中独立保存；
    # 片段和其他协调字段随后整体丢弃，避免迁移后继续重放旧计划。
    op.add_column(
        "runs",
        sa.Column(
            "memory_context_state",
            _json_type(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )
    # 旧片段一旦已有应用回执，去掉片段后整 Run 重提会重复已提交内容。
    # 不恢复旧分片协议，而是把仍 pending 的此类 Run 保守跳过并保留上下文。
    op.execute(
        "UPDATE runs AS run SET "
        "memory_extraction_status = 'skipped', "
        "memory_extracted_at = COALESCE(run.memory_extracted_at, CURRENT_TIMESTAMP), "
        "memory_extraction_safe_error_code = 'memory_legacy_partial', "
        "updated_at = CURRENT_TIMESTAMP "
        "WHERE run.memory_extraction_status = 'pending' "
        "AND EXISTS ("
        "SELECT 1 FROM jsonb_array_elements("
        "CASE WHEN jsonb_typeof(run.memory_extraction_state -> 'fragments') = 'array' "
        "THEN run.memory_extraction_state -> 'fragments' ELSE '[]'::jsonb END"
        ") AS fragment "
        "WHERE fragment ->> 'applied' = 'true' "
        "OR fragment ->> 'applied_batch_id' IS NOT NULL "
        "OR fragment ->> 'applied_at' IS NOT NULL "
        "OR fragment ->> 'safe_outcome' IS NOT NULL"
        ")"
    )
    op.execute(
        "UPDATE runs SET memory_context_state = CASE "
        "WHEN jsonb_typeof(memory_extraction_state -> 'context_snapshot') = 'object' "
        "THEN memory_extraction_state -> 'context_snapshot' "
        "ELSE '{}'::jsonb END"
    )
    # 同事务的数据更新可能留下 runs 的外键触发事件；先立即结算，
    # 否则 PostgreSQL 拒绝随后修改同一张表的列结构。
    op.execute("SET CONSTRAINTS ALL IMMEDIATE")
    op.drop_column("runs", "memory_extraction_state")

    # 首次启用边界从用户设置迁入固定扫描任务，扫描游标继续沿用。
    op.execute(
        "INSERT INTO memory_jobs "
        "(id, dedupe_key, kind, status, payload, result, namespace_id, scope_key, "
        "attempt_count, schema_retry_count, available_at, created_at, updated_at) "
        "SELECT 'mjob_' || substr(md5('scan_runs:global'), 1, 24), "
        "'scan_runs:global', 'scan_runs', 'queued', "
        "jsonb_build_object("
        "'kind', 'scan_runs', "
        "'cursor_finished_at', NULL, "
        "'cursor_run_id', NULL, "
        "'scan_round', 0, "
        "'enabled_boundary_at', COALESCE("
        "(SELECT MIN(auto_extract_boundary_at) FROM memory_settings "
        "WHERE auto_extract_boundary_at IS NOT NULL), CURRENT_TIMESTAMP)"
        "), NULL, '__global__', NULL, 0, 0, CURRENT_TIMESTAMP, "
        "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP "
        "WHERE NOT EXISTS ("
        "SELECT 1 FROM memory_jobs WHERE dedupe_key = 'scan_runs:global'"
        ") AND EXISTS ("
        "SELECT 1 FROM memory_settings WHERE auto_extract_boundary_at IS NOT NULL"
        ")"
    )
    op.execute(
        "UPDATE memory_jobs SET payload = payload || jsonb_build_object("
        "'enabled_boundary_at', COALESCE("
        "(SELECT MIN(auto_extract_boundary_at) FROM memory_settings "
        "WHERE auto_extract_boundary_at IS NOT NULL), CURRENT_TIMESTAMP)"
        ") WHERE kind = 'scan_runs' "
        "AND NOT (payload ? 'enabled_boundary_at')"
    )
    op.drop_column("memory_settings", "auto_extract_boundary_at")

    # 已完成 control 回执不能因升级失去幂等能力；只把旧 scope 版本投影为
    # 新 generation 字段，回执中仍不保存任何记忆正文。
    op.execute(
        "UPDATE memory_jobs SET result = jsonb_set("
        "result #- '{result,scope_revision}', "
        "'{result,generation}', "
        "to_jsonb(GREATEST(COALESCE("
        "(result #>> '{result,scope_revision}')::integer, 1"
        "), 1)), true) "
        "WHERE kind = 'control' AND status = 'done' "
        "AND result #> '{result,scope_revision}' IS NOT NULL"
    )

    # 旧提取任务可能保存模型候选正文；清空固定批次后由 Worker 重新读取
    # 当前 pending Run。done 任务保持 done，绝不重新执行已完成 Run。
    op.execute(
        "UPDATE memory_jobs SET payload = jsonb_build_object("
        "'kind', 'extract_pending', 'owner_id', namespace_id"
        ") WHERE kind = 'extract_pending'"
    )
    # cleanup 旧 payload 携带已删除的 scope epoch；升级只收敛 JSON 白名单，
    # 保留 namespace_id/scope_key，使新 extra=forbid 契约可以直接解析。
    op.execute(
        "UPDATE memory_jobs SET payload = payload - 'mutation_epoch' "
        "WHERE kind = 'cleanup_scope'"
    )
    op.drop_constraint("ck_memory_jobs_status", "memory_jobs", type_="check")
    op.execute(
        "UPDATE memory_jobs SET status = CASE status "
        "WHEN 'leased' THEN 'queued' "
        "WHEN 'dead' THEN 'failed' "
        "ELSE status END"
    )
    op.create_check_constraint(
        "ck_memory_jobs_status",
        "memory_jobs",
        "status IN ('queued','running','done','failed')",
    )
    op.drop_index("ix_memory_jobs_lease", table_name="memory_jobs")
    op.drop_column("memory_jobs", "worker_id")
    op.drop_column("memory_jobs", "lease_token")
    op.drop_column("memory_jobs", "lease_expires_at")

    op.drop_constraint("ck_memory_scope_revision", "memory_scope_state", type_="check")
    op.drop_constraint("ck_memory_scope_epoch", "memory_scope_state", type_="check")
    op.drop_column("memory_scope_state", "revision")
    op.drop_column("memory_scope_state", "mutation_epoch")
    op.drop_column("memory_scope_state", "extraction_not_before")


def downgrade() -> None:
    """恢复旧 schema 形状；不重建已删除正文，也不重放已完成 Run。"""
    op.add_column(
        "memory_scope_state",
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "memory_scope_state",
        sa.Column("mutation_epoch", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "memory_scope_state",
        sa.Column("extraction_not_before", sa.DateTime(timezone=True)),
    )
    op.create_check_constraint(
        "ck_memory_scope_revision",
        "memory_scope_state",
        "revision >= 0",
    )
    op.create_check_constraint(
        "ck_memory_scope_epoch",
        "memory_scope_state",
        "mutation_epoch >= 1",
    )
    # 旧 cleanup 契约要求 mutation_epoch；使用恢复后的 scope 行回填。
    # scope 行缺失时保留 1，与该列 server default 的兼容语义一致。
    op.execute(
        "UPDATE memory_jobs SET payload = jsonb_set("
        "payload, '{mutation_epoch}', to_jsonb(COALESCE(("
        "SELECT scope.mutation_epoch FROM memory_scope_state AS scope "
        "WHERE scope.namespace_id = memory_jobs.namespace_id "
        "AND scope.scope_key = memory_jobs.scope_key"
        "), 1)), true) "
        "WHERE kind = 'cleanup_scope'"
    )

    op.add_column("memory_jobs", sa.Column("worker_id", sa.String(128)))
    op.add_column(
        "memory_jobs",
        sa.Column("lease_token", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "memory_jobs",
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
    )
    op.drop_constraint("ck_memory_jobs_status", "memory_jobs", type_="check")
    op.execute(
        "UPDATE memory_jobs SET status = CASE status "
        "WHEN 'running' THEN 'leased' "
        "WHEN 'failed' THEN 'dead' "
        "ELSE status END"
    )
    op.create_check_constraint(
        "ck_memory_jobs_status",
        "memory_jobs",
        "status IN ('queued','leased','done','dead')",
    )
    op.create_index(
        "ix_memory_jobs_lease",
        "memory_jobs",
        ["status", "lease_expires_at"],
    )
    op.execute(
        "UPDATE memory_jobs SET result = jsonb_set("
        "result #- '{result,generation}', "
        "'{result,scope_revision}', "
        "to_jsonb(GREATEST(COALESCE("
        "(result #>> '{result,generation}')::integer, 0"
        "), 0)), true) "
        "WHERE kind = 'control' AND status = 'done' "
        "AND result #> '{result,generation}' IS NOT NULL"
    )

    op.add_column(
        "memory_settings",
        sa.Column("auto_extract_boundary_at", sa.DateTime(timezone=True)),
    )
    op.execute(
        "UPDATE memory_settings SET auto_extract_boundary_at = COALESCE("
        "(SELECT (payload ->> 'enabled_boundary_at')::timestamptz "
        "FROM memory_jobs WHERE kind = 'scan_runs' LIMIT 1), "
        "CURRENT_TIMESTAMP"
        ") WHERE auto_extract_enabled = true"
    )
    op.drop_constraint(
        "ck_memory_settings_generation",
        "memory_settings",
        type_="check",
    )
    op.drop_column("memory_settings", "generation")

    op.add_column(
        "runs",
        sa.Column(
            "memory_extraction_state",
            _json_type(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )
    op.execute(
        "UPDATE runs SET memory_extraction_state = jsonb_build_object("
        "'manifest_version', 1, "
        "'fragments', '[]'::jsonb, "
        "'context_snapshot', memory_context_state"
        ")"
    )
    op.execute("SET CONSTRAINTS ALL IMMEDIATE")
    op.drop_column("runs", "memory_context_state")
