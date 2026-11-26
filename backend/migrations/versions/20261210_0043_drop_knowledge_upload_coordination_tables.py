"""移除知识库上传预留与共享槽位协调表，改用无锁只读预检与进程内槽位限流。"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261210_0043"
down_revision: str | None = "20261209_0042"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """删除 knowledge_upload_slots 与 knowledge_upload_reservations 表及过期索引。"""
    op.drop_table("knowledge_upload_slots")
    op.drop_index(
        "ix_knowledge_upload_reservations_expiry",
        table_name="knowledge_upload_reservations",
    )
    op.drop_table("knowledge_upload_reservations")


def downgrade() -> None:
    """回滚重建上传预留表、过期索引及默认 4 个共享上传槽位行。"""
    op.create_table(
        "knowledge_upload_reservations",
        sa.Column(
            "owner_id",
            sa.String(128),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("operation", sa.String(16), primary_key=True),
        sa.Column("request_key", sa.String(128), primary_key=True),
        sa.Column("reservation_token", sa.String(64), nullable=False, unique=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("base_id", sa.String(64), nullable=False),
        sa.Column("document_id", sa.String(64), nullable=False),
        sa.Column("version_id", sa.String(64), nullable=False),
        sa.Column("source_id", sa.String(64), nullable=False),
        sa.Column("job_id", sa.String(64), nullable=False),
        sa.Column("storage_key", sa.String(256), nullable=False, unique=True),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("fencing_token", sa.Integer(), nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "operation IN ('create','replace')",
            name="ck_knowledge_upload_reservation_operation",
        ),
        sa.CheckConstraint(
            "state IN ('reserved','cleaning')",
            name="ck_knowledge_upload_reservation_state",
        ),
    )
    op.create_index(
        "ix_knowledge_upload_reservations_expiry",
        "knowledge_upload_reservations",
        ["state", "expires_at", "lease_until"],
    )
    op.create_table(
        "knowledge_upload_slots",
        sa.Column("slot_id", sa.Integer(), primary_key=True),
        sa.Column("token", sa.String(64)),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
    )
    op.bulk_insert(
        sa.table("knowledge_upload_slots", sa.column("slot_id", sa.Integer())),
        [{"slot_id": slot_id} for slot_id in range(1, 5)],
    )
