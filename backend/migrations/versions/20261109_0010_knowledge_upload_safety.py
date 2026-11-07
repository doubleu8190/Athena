"""Reserve knowledge upload quota and bound cross-instance upload staging."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261109_0010"
down_revision: str | None = "20261108_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
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
        sa.CheckConstraint("operation IN ('create','replace')"),
        sa.CheckConstraint("state IN ('reserved','cleaning')"),
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


def downgrade() -> None:
    op.drop_table("knowledge_upload_slots")
    op.drop_index(
        "ix_knowledge_upload_reservations_expiry", table_name="knowledge_upload_reservations"
    )
    op.drop_table("knowledge_upload_reservations")
