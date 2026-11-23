"""Add pgvector HNSW indexes for knowledge/memory and log reclamation index for skill executions."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261123_0032"
down_revision: str | None = "20261119_0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """为知识片段与长期记忆补充余弦相似度 HNSW 向量索引，为脚本日志回收补充部分索引。"""
    op.create_index(
        "ix_knowledge_chunks_embedding_hnsw",
        "knowledge_chunks",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
        postgresql_where=sa.text("embedding IS NOT NULL"),
    )
    op.create_index(
        "ix_memories_embedding_hnsw",
        "memories",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
        postgresql_where=sa.text("embedding IS NOT NULL AND deleted_time IS NULL"),
    )
    op.create_index(
        "ix_skill_executions_log_object_key",
        "skill_executions",
        ["log_object_key"],
        postgresql_where=sa.text("log_object_key IS NOT NULL"),
    )


def downgrade() -> None:
    """对称移除向量 HNSW 索引与脚本日志回收部分索引。"""
    op.drop_index("ix_skill_executions_log_object_key", table_name="skill_executions")
    op.drop_index("ix_memories_embedding_hnsw", table_name="memories")
    op.drop_index("ix_knowledge_chunks_embedding_hnsw", table_name="knowledge_chunks")
