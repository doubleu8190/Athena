"""记忆模块 PostgreSQL ORM 映射。"""

from __future__ import annotations

from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Mapped, mapped_column, relationship

from adapter.base import Base


@compiles(TSVECTOR, "sqlite")
def _compile_tsvector_for_sqlite(_type: TSVECTOR, _compiler: object, **_kwargs: object) -> str:
    """让共用 ORM 元数据可供 SQLite 结构测试建表。"""
    return "TEXT"


def _json_type() -> JSON:
    """在 PostgreSQL 使用 JSONB，在 SQLite 测试中使用 JSON。"""
    return JSON().with_variant(JSONB(), "postgresql")


class MemoryModel(Base):
    """保存记忆正文及其同步生成的检索字段。"""

    __tablename__ = "memories"
    __table_args__ = (
        UniqueConstraint("namespace_id", "request_id", name="uq_memories_request"),
        Index("ix_memories_scope_active", "namespace_id", "scope_key", "updated_at"),
        Index(
            "ix_memories_context",
            "namespace_id",
            "scope_key",
            "status",
            "load_policy",
            "expires_at",
        ),
        Index(
            "ix_memories_content_hash",
            "namespace_id",
            "scope_key",
            "content_hash",
        ),
        CheckConstraint(
            "status IN ('pending','active','archived','superseded')",
            name="ck_memories_status",
        ),
        CheckConstraint(
            "origin IS NULL OR origin IN ('manual','explicit','extracted','')",
            name="ck_memories_origin",
        ),
        CheckConstraint(
            "kind IS NULL OR kind IN ('constraint','decision','preference','fact')",
            name="ck_memories_kind",
        ),
        CheckConstraint(
            "load_policy IS NULL OR load_policy IN ('always','on_demand')",
            name="ck_memories_load_policy",
        ),
        Index(
            "ix_memories_content_fts",
            "search_document",
            postgresql_using="gin",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    namespace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    scope_key: Mapped[str] = mapped_column(String(128), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    search_document: Mapped[str] = mapped_column(TSVECTOR, nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1024), nullable=True)
    embedding_profile_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    origin: Mapped[str | None] = mapped_column(String(16), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    kind: Mapped[str | None] = mapped_column(String(32), nullable=True, default="fact")
    load_policy: Mapped[str | None] = mapped_column(String(16), nullable=True, default="on_demand")
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    explicit_long_term: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=False)
    last_evidenced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    superseded_by_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("memories.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deleted_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sources: Mapped[list[MemorySourceModel]] = relationship(
        back_populates="memory", cascade="all, delete-orphan", lazy="selectin"
    )


class MemorySourceModel(Base):
    """保存记忆的通用来源引用。"""

    __tablename__ = "memory_sources"
    __table_args__ = (
        UniqueConstraint("memory_id", "source_kind", "source_ref"),
        Index("ix_memory_sources_source_at", "memory_id", "source_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    memory_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("memories.id", ondelete="CASCADE"), nullable=False
    )
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source_ref: Mapped[str] = mapped_column(String(256), nullable=False)
    source_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    memory: Mapped[MemoryModel] = relationship(back_populates="sources")


class MemorySettingModel(Base):
    """保存用户级设置和提取失效代次。"""

    __tablename__ = "memory_settings"
    # 与迁移 20261125_0026 保持一致，确保用户级失效代次始终从 1 起步。
    __table_args__ = (
        CheckConstraint("generation >= 1", name="ck_memory_settings_generation"),
    )

    namespace_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    read_enabled: Mapped[bool] = mapped_column(nullable=False, default=True)
    auto_extract_enabled: Mapped[bool] = mapped_column(nullable=False, default=True)
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class MemoryScopeStateModel(Base):
    """保存单个授权 scope 的启用状态和归档进度。"""

    __tablename__ = "memory_scope_state"
    __table_args__ = (CheckConstraint("enabled IN (true,false)", name="ck_memory_scope_enabled"),)

    namespace_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    scope_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_maintained_day: Mapped[str | None] = mapped_column(String(10), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MemoryJobModel(Base):
    """保存后台任务、固定扫描记录和同步控制回执。"""

    __tablename__ = "memory_jobs"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_memory_jobs_dedupe"),
        CheckConstraint(
            "kind IN ('extract_pending','maintain_scope','cleanup_scope','control','scan_runs')",
            name="ck_memory_jobs_kind",
        ),
        CheckConstraint(
            "status IN ('queued','running','done','failed')",
            name="ck_memory_jobs_status",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_memory_jobs_attempt_count"),
        CheckConstraint("schema_retry_count >= 0", name="ck_memory_jobs_schema_retry"),
        Index("ix_memory_jobs_claim", "kind", "status", "available_at", "id"),
        Index("ix_memory_jobs_scope_kind", "namespace_id", "scope_key", "kind"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    dedupe_key: Mapped[str] = mapped_column(String(256), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(_json_type(), nullable=False)
    result: Mapped[dict[str, object] | None] = mapped_column(_json_type(), nullable=True)
    namespace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    scope_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    schema_retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


__all__ = [
    "MemoryJobModel",
    "MemoryModel",
    "MemoryScopeStateModel",
    "MemorySettingModel",
    "MemorySourceModel",
]
