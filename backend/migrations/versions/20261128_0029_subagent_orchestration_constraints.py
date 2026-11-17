"""扩展 runs 表的 Subagent 编排约束、活跃唯一索引与冻结参数列。"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261128_0029"
down_revision: str | None = "20261127_0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """升级 runs 状态约束、按 depth=0 隔离活跃会话索引，并新增子 Run 冻结配置列。"""
    op.add_column("runs", sa.Column("subagent_system_prompt", sa.Text(), nullable=True))
    op.add_column("runs", sa.Column("max_llm_requests", sa.Integer(), nullable=True))
    op.add_column("runs", sa.Column("timeout_seconds", sa.Integer(), nullable=True))

    op.drop_constraint("ck_runs_status", "runs", type_="check")
    op.create_check_constraint(
        "ck_runs_status",
        "runs",
        "status IN ('queued', 'running', 'waiting_for_input', "
        "'waiting_for_approval', 'waiting_for_subagents', 'cancelling', "
        "'completed', 'failed', 'cancelled')",
    )

    op.drop_constraint("ck_runs_llm_request_count", "runs", type_="check")
    op.create_check_constraint(
        "ck_runs_llm_request_count",
        "runs",
        "llm_request_count >= 0 AND llm_request_count <= 50",
    )

    op.drop_index("uq_runs_active_session", table_name="runs")
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


def downgrade() -> None:
    """回滚 runs 表的 Subagent 编排约束与冻结参数列。"""
    op.drop_index("uq_runs_active_session", table_name="runs")
    op.create_index(
        "uq_runs_active_session",
        "runs",
        ["session_id"],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('queued', 'running', 'waiting_for_input', "
            "'waiting_for_approval', 'cancelling')"
        ),
    )

    op.drop_constraint("ck_runs_llm_request_count", "runs", type_="check")
    op.create_check_constraint(
        "ck_runs_llm_request_count",
        "runs",
        "llm_request_count >= 0 AND llm_request_count <= 40",
    )

    op.drop_constraint("ck_runs_status", "runs", type_="check")
    op.create_check_constraint(
        "ck_runs_status",
        "runs",
        "status IN ('queued', 'running', 'waiting_for_input', "
        "'waiting_for_approval', 'cancelling', 'completed', 'failed', 'cancelled')",
    )

    op.drop_column("runs", "timeout_seconds")
    op.drop_column("runs", "max_llm_requests")
    op.drop_column("runs", "subagent_system_prompt")
