"""记忆模块 PostgreSQL ORM 映射。"""

from __future__ import annotations

from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Mapped, mapped_column, relationship

from adapter.base import Base


@compiles(TSVECTOR, "sqlite")
def _compile_tsvector_for_sqlite(_type: TSVECTOR, _compiler: object, **_kwargs: object) -> str:
    """让共用 ORM 元数据可供 SQLite 结构测试建表。"""
    return "TEXT"


class MemoryModel(Base):
    """保存记忆正文及其同步生成的检索字段。"""

    __tablename__ = "memories"
    __table_args__ = (
        UniqueConstraint("namespace_id", "request_id", name="uq_memories_request"),
        Index("ix_memories_scope_active", "namespace_id", "scope_key", "updated_at"),
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
    embedding_profile_id: Mapped[str] = mapped_column(String(128), nullable=False)
    request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    origin: Mapped[str] = mapped_column(String(16), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deleted_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sources: Mapped[list[MemorySourceModel]] = relationship(
        back_populates="memory", cascade="all, delete-orphan", lazy="selectin"
    )


class MemorySourceModel(Base):
    """保存记忆的通用来源引用。"""

    __tablename__ = "memory_sources"
    __table_args__ = (UniqueConstraint("memory_id", "source_kind", "source_ref"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    memory_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("memories.id", ondelete="CASCADE"), nullable=False
    )
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source_ref: Mapped[str] = mapped_column(String(256), nullable=False)
    memory: Mapped[MemoryModel] = relationship(back_populates="sources")


class MemorySettingModel(Base):
    """保存 namespace 级读取和提取设置。"""

    __tablename__ = "memory_settings"

    namespace_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    read_enabled: Mapped[bool] = mapped_column(nullable=False, default=True)
    auto_extract_enabled: Mapped[bool] = mapped_column(nullable=False, default=True)


__all__ = ["MemoryModel", "MemorySettingModel", "MemorySourceModel"]
