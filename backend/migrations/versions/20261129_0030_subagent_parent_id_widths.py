"""修正已应用旧版父 Run 关联列的宽度。"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

revision: str = "20261129_0030"
down_revision: str | None = "20261128_0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TARGET_WIDTHS = {
    "parent_run_id": 128,
    "root_run_id": 128,
    "parent_tool_call_id": 256,
}


def upgrade() -> None:
    """兼容旧版 64 列宽，并将三个父 Run 关联列安全扩宽到目标宽度。"""
    if context.is_offline_mode():
        # 离线 SQL 无法读取现场元数据，扩宽操作本身不会截断已有数据。
        for column_name, target_width in _TARGET_WIDTHS.items():
            op.alter_column(
                "runs",
                column_name,
                type_=sa.String(length=target_width),
                existing_nullable=True,
            )
        return

    connection = op.get_bind()
    columns = {
        column["name"]: column
        for column in sa.inspect(connection).get_columns("runs")
    }
    current_widths: dict[str, int] = {}
    for column_name, target_width in _TARGET_WIDTHS.items():
        column = columns.get(column_name)
        if column is None:
            raise RuntimeError(f"subagent_parent_id_column_missing:{column_name}")
        column_type = column["type"]
        current_width = (
            column_type.length
            if isinstance(column_type, sa.String)
            else None
        )
        if not isinstance(current_width, int):
            raise RuntimeError(f"subagent_parent_id_width_invalid:{column_name}")
        if current_width > target_width:
            raise RuntimeError(
                "subagent_parent_id_width_unsafe:"
                f"{column_name}:{current_width}>{target_width}"
            )
        current_widths[column_name] = current_width

    # 先完成全部宽度校验，再逐列扩宽，避免一个异常列导致事务内出现部分变更。
    for column_name, target_width in _TARGET_WIDTHS.items():
        current_width = current_widths[column_name]
        if current_width == target_width:
            continue
        op.alter_column(
            "runs",
            column_name,
            existing_type=sa.String(length=current_width),
            type_=sa.String(length=target_width),
            existing_nullable=columns[column_name]["nullable"],
        )


def downgrade() -> None:
    """保留已扩宽的列，避免降级时截断长 ID 或破坏现有数据。"""
    return None
