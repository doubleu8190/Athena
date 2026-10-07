"""Files/Knowledge 的 PostgreSQL ORM 模型。"""

from __future__ import annotations

from sqlalchemy import CheckConstraint, Computed, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column
from pgvector.sqlalchemy import Vector

from .base import Base


class KnowledgeBaseModel(Base):
    """knowledge_bases 表映射。"""

    __tablename__ = "knowledge_bases"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String)
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[str] = mapped_column(String)
    updated_at: Mapped[str] = mapped_column(String)
    deleted_time: Mapped[str | None] = mapped_column(String, nullable=True)


class AttachmentModel(Base):
    """attachments 表映射。"""

    __tablename__ = "attachments"
    __table_args__ = (
        Index("idx_attachments_session", "session_id"),
        Index("idx_attachments_knowledge_base", "knowledge_base_id"),
        Index("idx_attachments_message", "message_id"),
        Index("idx_attachments_hash", "sha256"),
        Index("idx_attachments_status", "status"),
        CheckConstraint(
            "(session_id IS NOT NULL) != (knowledge_base_id IS NOT NULL)",
            name="ck_attachment_single_owner",
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    logical_document_id: Mapped[str] = mapped_column(String)
    document_version: Mapped[int] = mapped_column(Integer, default=1)
    session_id: Mapped[str | None] = mapped_column(String, nullable=True)
    knowledge_base_id: Mapped[str | None] = mapped_column(
        ForeignKey("knowledge_bases.id"), nullable=True
    )
    message_id: Mapped[str | None] = mapped_column(String, nullable=True)
    filename: Mapped[str] = mapped_column(String)
    mime_type: Mapped[str] = mapped_column(String)
    size_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String)
    storage_key: Mapped[str] = mapped_column(String)
    adapter_name: Mapped[str | None] = mapped_column(String, nullable=True)
    adapter_version: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="uploaded")
    capabilities_json: Mapped[str] = mapped_column(Text, default="[]")
    parsed_metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String)
    updated_at: Mapped[str] = mapped_column(String)
    deleted_time: Mapped[str | None] = mapped_column(String, nullable=True)


class FileChunkModel(Base):
    """file_chunks 表映射。"""

    __tablename__ = "file_chunks"
    __table_args__ = (
        Index(
            "uq_file_chunk_attachment_ordinal_active",
            "attachment_id",
            "ordinal",
            unique=True,
            postgresql_where="deleted_time IS NULL",
        ),
        Index("idx_file_chunks_attachment", "attachment_id"),
        Index("idx_file_chunks_content_fts", "content_fts", postgresql_using="gin"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    attachment_id: Mapped[str] = mapped_column(ForeignKey("attachments.id"))
    ordinal: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1024), nullable=True)
    content_fts: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('chinese'::regconfig, content)", persisted=True),
    )
    token_count: Mapped[int] = mapped_column(Integer, default=0)
    locator_json: Mapped[str] = mapped_column(Text, default="{}")
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    native_score: Mapped[float | None] = mapped_column(nullable=True)
    deleted_time: Mapped[str | None] = mapped_column(String, nullable=True)


class FileArtifactModel(Base):
    """file_artifacts 表映射。"""

    __tablename__ = "file_artifacts"
    __table_args__ = (
        Index("uq_file_artifact_cache_key", "cache_key", unique=True),
        Index("idx_file_artifacts_attachment", "attachment_id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    attachment_id: Mapped[str] = mapped_column(ForeignKey("attachments.id"))
    kind: Mapped[str] = mapped_column(String)
    cache_key: Mapped[str] = mapped_column(String)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    storage_key: Mapped[str | None] = mapped_column(String, nullable=True)
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[str] = mapped_column(String)


class AdapterRegistryModel(Base):
    """adapter_registry 表映射。"""

    __tablename__ = "adapter_registry"

    name: Mapped[str] = mapped_column(String, primary_key=True)
    version: Mapped[str] = mapped_column(String)
    mime_types_json: Mapped[str] = mapped_column(Text, default="[]")
    extensions_json: Mapped[str] = mapped_column(Text, default="[]")
    capabilities_json: Mapped[str] = mapped_column(Text, default="[]")
    enabled: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[str] = mapped_column(String)
