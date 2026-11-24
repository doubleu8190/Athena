"""M3.1 会话附件 PDF / Office 提取文本缓存列与一致性约束。"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261205_0038"
down_revision: str | None = "20261204_0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_EXTRACTION_CONSISTENCY_SQL = (
    "(extraction_status = 'none' AND extracted_text_key IS NULL "
    "AND extracted_text_bytes IS NULL AND extraction_error_code IS NULL) OR "
    "(extraction_status = 'ready' AND extracted_text_key IS NOT NULL "
    "AND extracted_text_bytes IS NOT NULL AND extraction_error_code IS NULL) OR "
    "(extraction_status = 'failed' AND extracted_text_key IS NULL "
    "AND extracted_text_bytes IS NULL AND extraction_error_code IS NOT NULL)"
)


def upgrade() -> None:
    """为 session_files 新增提取状态、缓存对象键、字节数、错误码列及一致性约束。"""
    # 使用 batch_alter_table：PostgreSQL（含离线 SQL 模式）直接生成标准 ALTER TABLE，
    # SQLite 在线迁移自动通过表重建施加 CheckConstraint，兼顾双引擎一致性。
    with op.batch_alter_table("session_files") as batch_op:
        batch_op.add_column(
            sa.Column(
                "extraction_status",
                sa.String(length=16),
                nullable=False,
                server_default=sa.text("'none'"),
            )
        )
        batch_op.add_column(
            sa.Column(
                "extracted_text_key",
                sa.String(length=1024),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "extracted_text_bytes",
                sa.BigInteger(),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "extraction_error_code",
                sa.String(length=64),
                nullable=True,
            )
        )
        batch_op.create_check_constraint(
            "ck_session_files_extraction_status",
            "extraction_status IN ('none', 'ready', 'failed')",
        )
        batch_op.create_check_constraint(
            "ck_session_files_extracted_text_bytes",
            "extracted_text_bytes IS NULL OR extracted_text_bytes >= 0",
        )
        batch_op.create_check_constraint(
            "ck_session_files_extraction_consistency",
            _EXTRACTION_CONSISTENCY_SQL,
        )
        # 唯一索引同时保证提取缓存对象键唯一性，并加速离线对象回收时的引用复核查询。
        batch_op.create_index(
            "uq_session_files_extracted_text_key",
            ["extracted_text_key"],
            unique=True,
        )


def downgrade() -> None:
    """按逆序回滚 session_files 的提取缓存索引、约束及 4 个新增列。"""
    with op.batch_alter_table("session_files") as batch_op:
        batch_op.drop_index("uq_session_files_extracted_text_key")
        batch_op.drop_constraint(
            "ck_session_files_extraction_consistency",
            type_="check",
        )
        batch_op.drop_constraint(
            "ck_session_files_extracted_text_bytes",
            type_="check",
        )
        batch_op.drop_constraint(
            "ck_session_files_extraction_status",
            type_="check",
        )
        batch_op.drop_column("extraction_error_code")
        batch_op.drop_column("extracted_text_bytes")
        batch_op.drop_column("extracted_text_key")
        batch_op.drop_column("extraction_status")
