"""create knowledge tables"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "20261108_0009"
down_revision: str | None = "20261107_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "knowledge_bases",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "owner_id",
            sa.String(128),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("description", sa.String(500)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_time", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("id", "owner_id", name="uq_knowledge_bases_id_owner"),
    )
    op.create_index(
        "ix_knowledge_bases_owner_updated", "knowledge_bases", ["owner_id", "updated_at"]
    )
    op.create_table(
        "knowledge_sources",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "owner_id",
            sa.String(128),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("storage_key", sa.String(256), nullable=False, unique=True),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("media_type", sa.String(128), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("id", "owner_id", name="uq_knowledge_sources_id_owner"),
    )
    op.create_table(
        "knowledge_documents",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("owner_id", sa.String(128), nullable=False),
        sa.Column("knowledge_base_id", sa.String(64), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("active_version_id", sa.String(64)),
        sa.Column("pending_version_id", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_time", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("id", "owner_id", name="uq_knowledge_documents_id_owner"),
        sa.ForeignKeyConstraint(
            ["knowledge_base_id", "owner_id"],
            ["knowledge_bases.id", "knowledge_bases.owner_id"],
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_knowledge_documents_owner_kb",
        "knowledge_documents",
        ["owner_id", "knowledge_base_id", "updated_at"],
    )
    op.create_table(
        "knowledge_document_versions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("document_id", sa.String(64), nullable=False),
        sa.Column("owner_id", sa.String(128), nullable=False),
        sa.Column("source_id", sa.String(64)),
        sa.Column("source_text", sa.Text()),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("profile_id", sa.String(128), nullable=False),
        sa.Column("processing_config", postgresql.JSONB(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("error_code", sa.String(64)),
        sa.Column("warnings", postgresql.JSONB(), nullable=False),
        sa.Column("chunk_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("id", "document_id", name="uq_knowledge_versions_id_document"),
        sa.ForeignKeyConstraint(
            ["document_id", "owner_id"],
            ["knowledge_documents.id", "knowledge_documents.owner_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_id", "owner_id"], ["knowledge_sources.id", "knowledge_sources.owner_id"]
        ),
        sa.CheckConstraint(
            "(source_id IS NULL) <> (source_text IS NULL)", name="ck_knowledge_version_source"
        ),
        sa.CheckConstraint(
            "state IN ('queued','processing','ready','failed')", name="ck_knowledge_version_state"
        ),
    )
    op.create_table(
        "knowledge_chunks",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("document_id", sa.String(64), nullable=False),
        sa.Column("version_id", sa.String(64), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("locator", postgresql.JSONB(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("search_document", postgresql.TSVECTOR(), nullable=False),
        sa.Column("embedding", Vector(1024)),
        sa.UniqueConstraint("version_id", "ordinal", name="uq_knowledge_chunks_version_ordinal"),
        sa.ForeignKeyConstraint(
            ["version_id", "document_id"],
            ["knowledge_document_versions.id", "knowledge_document_versions.document_id"],
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_knowledge_chunks_version", "knowledge_chunks", ["version_id"])
    op.create_index(
        "ix_knowledge_chunks_fts", "knowledge_chunks", ["search_document"], postgresql_using="gin"
    )
    op.create_table(
        "knowledge_jobs",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "owner_id",
            sa.String(128),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("target_id", sa.String(64), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("fencing_token", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("error_code", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "state IN ('queued','processing','retry','done','failed')",
            name="ck_knowledge_jobs_state",
        ),
    )
    op.create_index("ix_knowledge_jobs_ready", "knowledge_jobs", ["state", "next_attempt_at"])
    op.create_index("ix_knowledge_jobs_lease", "knowledge_jobs", ["state", "lease_until"])
    op.create_table(
        "knowledge_requests",
        sa.Column(
            "owner_id",
            sa.String(128),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("operation", sa.String(16), primary_key=True),
        sa.Column("request_key", sa.String(128), primary_key=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("document_id", sa.String(64), nullable=False),
        sa.Column("version_id", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "project_knowledge_bases",
        sa.Column("project_id", sa.String(64), primary_key=True),
        sa.Column("knowledge_base_id", sa.String(64), primary_key=True),
        sa.Column("owner_id", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id", "owner_id"], ["projects.id", "projects.owner_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["knowledge_base_id", "owner_id"],
            ["knowledge_bases.id", "knowledge_bases.owner_id"],
            ondelete="CASCADE",
        ),
    )


def downgrade() -> None:
    for table in (
        "project_knowledge_bases",
        "knowledge_requests",
        "knowledge_jobs",
        "knowledge_chunks",
        "knowledge_document_versions",
        "knowledge_documents",
        "knowledge_sources",
        "knowledge_bases",
    ):
        op.drop_table(table)
