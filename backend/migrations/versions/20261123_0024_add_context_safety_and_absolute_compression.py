"""增加绝对压缩阈值、Token 上限和安全失败报告引用。"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261123_0024"
down_revision: str | None = "20261122_0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """保留旧策略列和主键引用，只增加可为空的明确新格式字段。"""
    op.add_column(
        "llm_compression_policy_versions",
        sa.Column("trigger_tokens", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "llm_compression_policy_versions",
        sa.Column("recent_token_cap", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "llm_compression_policy_versions",
        sa.Column("summary_token_cap", sa.BigInteger(), nullable=True),
    )
    op.create_check_constraint(
        "ck_compression_trigger_tokens",
        "llm_compression_policy_versions",
        "trigger_tokens IS NULL OR trigger_tokens > 0",
    )
    op.create_check_constraint(
        "ck_compression_recent_token_cap",
        "llm_compression_policy_versions",
        "recent_token_cap IS NULL OR recent_token_cap > 0",
    )
    op.create_check_constraint(
        "ck_compression_summary_token_cap",
        "llm_compression_policy_versions",
        "summary_token_cap IS NULL OR summary_token_cap > 0",
    )
    op.add_column("runs", sa.Column("report_message_id", sa.String(64), nullable=True))
    op.create_foreign_key(
        "fk_runs_report_message_session",
        "runs",
        "conversation_messages",
        ["report_message_id", "session_id"],
        ["id", "session_id"],
        ondelete="RESTRICT",
        deferrable=True,
        initially="DEFERRED",
    )


def downgrade() -> None:
    """删除新投影；不把绝对 Token 规则反推成历史浮点比例。"""
    op.drop_constraint("fk_runs_report_message_session", "runs", type_="foreignkey")
    op.drop_column("runs", "report_message_id")
    op.drop_constraint(
        "ck_compression_summary_token_cap",
        "llm_compression_policy_versions",
        type_="check",
    )
    op.drop_constraint(
        "ck_compression_recent_token_cap",
        "llm_compression_policy_versions",
        type_="check",
    )
    op.drop_constraint(
        "ck_compression_trigger_tokens",
        "llm_compression_policy_versions",
        type_="check",
    )
    op.drop_column("llm_compression_policy_versions", "summary_token_cap")
    op.drop_column("llm_compression_policy_versions", "recent_token_cap")
    op.drop_column("llm_compression_policy_versions", "trigger_tokens")
