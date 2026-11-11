"""Create immutable system prompt versions and bind Run/compression snapshots."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

from domain.system_prompt.defaults import (
    INITIAL_COMPRESSION_SYSTEM_PROMPT,
    INITIAL_MAIN_SYSTEM_PROMPT,
    INITIAL_SYSTEM_PROMPT_IDS,
)

revision: str = "20261116_0017"
down_revision: str | None = "20261115_0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """初始化两类原文，并把既有 Run/摘要解释为该初始正文版本。"""
    op.create_table(
        "system_prompt_versions",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_by", sa.String(128)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("restored_from_version_id", sa.String(128)),
        sa.CheckConstraint(
            "kind IN ('main', 'compression')",
            name="ck_system_prompt_versions_kind",
        ),
        sa.UniqueConstraint(
            "id",
            "kind",
            name="uq_system_prompt_versions_id_kind",
        ),
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

    initialized_at = datetime.now(UTC)
    versions = sa.table(
        "system_prompt_versions",
        sa.column("id", sa.String),
        sa.column("kind", sa.String),
        sa.column("content", sa.Text),
        sa.column("created_by", sa.String),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("restored_from_version_id", sa.String),
    )
    op.bulk_insert(
        versions,
        [
            {
                "id": INITIAL_SYSTEM_PROMPT_IDS["main"],
                "kind": "main",
                "content": INITIAL_MAIN_SYSTEM_PROMPT,
                "created_by": None,
                "created_at": initialized_at,
                "restored_from_version_id": None,
            },
            {
                "id": INITIAL_SYSTEM_PROMPT_IDS["compression"],
                "kind": "compression",
                "content": INITIAL_COMPRESSION_SYSTEM_PROMPT,
                "created_by": None,
                "created_at": initialized_at,
                "restored_from_version_id": None,
            },
        ],
    )
    settings = sa.table(
        "system_prompt_settings",
        sa.column("kind", sa.String),
        sa.column("current_version_id", sa.String),
    )
    op.bulk_insert(
        settings,
        [
            {
                "kind": "main",
                "current_version_id": INITIAL_SYSTEM_PROMPT_IDS["main"],
            },
            {
                "kind": "compression",
                "current_version_id": INITIAL_SYSTEM_PROMPT_IDS["compression"],
            },
        ],
    )

    # 先回填既有记录再收紧非空约束，确保升级兼容历史 Run 和未发布摘要。
    op.add_column("runs", sa.Column("main_prompt_version_id", sa.String(128)))
    op.add_column("runs", sa.Column("compression_prompt_version_id", sa.String(128)))
    op.execute(
        sa.text(
            "UPDATE runs SET main_prompt_version_id = :main_id, "
            "compression_prompt_version_id = :compression_id"
        ).bindparams(
            main_id=INITIAL_SYSTEM_PROMPT_IDS["main"],
            compression_id=INITIAL_SYSTEM_PROMPT_IDS["compression"],
        )
    )
    op.alter_column("runs", "main_prompt_version_id", nullable=False)
    op.alter_column("runs", "compression_prompt_version_id", nullable=False)
    op.create_foreign_key(
        "fk_runs_main_prompt_version",
        "runs",
        "system_prompt_versions",
        ["main_prompt_version_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_runs_compression_prompt_version",
        "runs",
        "system_prompt_versions",
        ["compression_prompt_version_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    op.add_column(
        "session_context_summaries",
        sa.Column("compression_prompt_version_id", sa.String(128)),
    )
    op.execute(
        sa.text(
            "UPDATE session_context_summaries "
            "SET compression_prompt_version_id = :compression_id"
        ).bindparams(compression_id=INITIAL_SYSTEM_PROMPT_IDS["compression"])
    )
    op.alter_column(
        "session_context_summaries",
        "compression_prompt_version_id",
        nullable=False,
    )
    op.create_foreign_key(
        "fk_context_summaries_compression_prompt",
        "session_context_summaries",
        "system_prompt_versions",
        ["compression_prompt_version_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    """移除内容版本快照，保留既有 Run、摘要及模型协议字段。"""
    op.drop_constraint(
        "fk_context_summaries_compression_prompt",
        "session_context_summaries",
        type_="foreignkey",
    )
    op.drop_column("session_context_summaries", "compression_prompt_version_id")
    op.drop_constraint(
        "fk_runs_compression_prompt_version",
        "runs",
        type_="foreignkey",
    )
    op.drop_constraint("fk_runs_main_prompt_version", "runs", type_="foreignkey")
    op.drop_column("runs", "compression_prompt_version_id")
    op.drop_column("runs", "main_prompt_version_id")
    op.drop_table("system_prompt_settings")
    op.drop_index(
        "ix_system_prompt_versions_kind_created_id",
        table_name="system_prompt_versions",
    )
    op.drop_table("system_prompt_versions")
