"""知识库 PostgreSQL ORM。"""

from __future__ import annotations

from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from adapter.base import Base


class KnowledgeBaseModel(Base):
    __tablename__ = "knowledge_bases"
    __table_args__ = (
        UniqueConstraint("id", "owner_id", name="uq_knowledge_bases_id_owner"),
        CheckConstraint(
            "visibility_scope IN ('company', 'department', 'personal')",
            name="ck_knowledge_bases_visibility_scope",
        ),
        CheckConstraint(
            "(visibility_scope = 'department' AND owner_department_id IS NOT NULL) "
            "OR (visibility_scope IN ('company', 'personal') AND owner_department_id IS NULL)",
            name="ck_knowledge_bases_owner_department_scope",
        ),
        Index("ix_knowledge_bases_owner_updated", "owner_id", "updated_at"),
        Index(
            "ix_knowledge_bases_visibility_dept",
            "visibility_scope",
            "owner_department_id",
            "deleted_time",
        ),
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500))
    # 三层可见范围：默认 personal，兼容既有创建路径；department 级必须绑定活跃归属部门。
    visibility_scope: Mapped[str] = mapped_column(
        String(16), nullable=False, default="personal", server_default="personal"
    )
    owner_department_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("departments.id", ondelete="RESTRICT"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deleted_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class KnowledgeSourceModel(Base):
    __tablename__ = "knowledge_sources"
    __table_args__ = (
        UniqueConstraint("id", "owner_id", name="uq_knowledge_sources_id_owner"),
        # 加速按 owner_id 统计与检索用户上传原件。
        Index("ix_knowledge_sources_owner_id", "owner_id"),
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    storage_key: Mapped[str] = mapped_column(String(256), unique=True)
    filename: Mapped[str] = mapped_column(String(255))
    media_type: Mapped[str] = mapped_column(String(128))
    size_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class KnowledgeDocumentModel(Base):
    __tablename__ = "knowledge_documents"
    __table_args__ = (
        UniqueConstraint("id", "owner_id", name="uq_knowledge_documents_id_owner"),
        # 拆除 (knowledge_base_id, owner_id) 复合外键，改为单列外键以支持同部门多管理员协作维护。
        ForeignKeyConstraint(
            ["knowledge_base_id"],
            ["knowledge_bases.id"],
            name="fk_knowledge_documents_base",
            ondelete="CASCADE",
        ),
        Index("ix_knowledge_documents_owner_kb", "owner_id", "knowledge_base_id", "updated_at"),
        # 加速共享知识库按更新时间倒序分页查询未删除文档。
        Index(
            "ix_knowledge_documents_kb_active_updated",
            "knowledge_base_id",
            text("updated_at DESC"),
            text("id DESC"),
            postgresql_where=text("deleted_time IS NULL"),
            sqlite_where=text("deleted_time IS NULL"),
        ),
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # 语义明确为文档创建/上传者 ID，不再与所属知识库 owner_id 强制绑定。
    owner_id: Mapped[str] = mapped_column(String(128), nullable=False)
    knowledge_base_id: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    active_version_id: Mapped[str | None] = mapped_column(String(64))
    pending_version_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deleted_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class KnowledgeVersionModel(Base):
    __tablename__ = "knowledge_document_versions"
    __table_args__ = (
        UniqueConstraint("id", "document_id", name="uq_knowledge_versions_id_document"),
        # 拆除 (document_id, owner_id) 与 (source_id, owner_id) 复合外键，允许协作替换版本。
        ForeignKeyConstraint(
            ["document_id"],
            ["knowledge_documents.id"],
            name="fk_knowledge_versions_document",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["source_id"],
            ["knowledge_sources.id"],
            name="fk_knowledge_versions_source",
        ),
        CheckConstraint(
            "(source_id IS NULL) != (source_text IS NULL)", name="ck_knowledge_version_source"
        ),
        CheckConstraint(
            "state IN ('queued','processing','ready','failed')", name="ck_knowledge_version_state"
        ),
        # 加速按文档倒序查看版本历史及按原件反查引用关系。
        Index(
            "ix_knowledge_versions_doc_created",
            "document_id",
            text("created_at DESC"),
            text("id DESC"),
        ),
        Index(
            "ix_knowledge_versions_source_id",
            "source_id",
            postgresql_where=text("source_id IS NOT NULL"),
            sqlite_where=text("source_id IS NOT NULL"),
        ),
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    document_id: Mapped[str] = mapped_column(String(64), nullable=False)
    owner_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_id: Mapped[str | None] = mapped_column(String(64))
    source_text: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    profile_id: Mapped[str] = mapped_column(String(128), nullable=False)
    processing_config: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    warnings: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class KnowledgeChunkModel(Base):
    __tablename__ = "knowledge_chunks"
    __table_args__ = (
        UniqueConstraint("version_id", "ordinal", name="uq_knowledge_chunks_version_ordinal"),
        ForeignKeyConstraint(
            ["version_id", "document_id"],
            ["knowledge_document_versions.id", "knowledge_document_versions.document_id"],
            ondelete="CASCADE",
        ),
        Index("ix_knowledge_chunks_fts", "search_document", postgresql_using="gin"),
        # 与迁移 20261123_0032 对齐：PostgreSQL 上使用 pgvector HNSW 余弦距离部分索引。
        Index(
            "ix_knowledge_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
            postgresql_where=text("embedding IS NOT NULL"),
        ),
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    document_id: Mapped[str] = mapped_column(String(64), nullable=False)
    version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    locator: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    search_document: Mapped[str] = mapped_column(TSVECTOR, nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1024))


class KnowledgeJobModel(Base):
    __tablename__ = "knowledge_jobs"
    __table_args__ = (
        CheckConstraint(
            "state IN ('queued','processing','retry','done','failed')",
            name="ck_knowledge_jobs_state",
        ),
        Index("ix_knowledge_jobs_ready", "state", "next_attempt_at"),
        Index("ix_knowledge_jobs_lease", "state", "lease_until"),
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    target_id: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fencing_token: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class KnowledgeRequestModel(Base):
    __tablename__ = "knowledge_requests"
    __table_args__ = (
        UniqueConstraint("owner_id", "operation", "request_key", name="uq_knowledge_request_key"),
    )
    owner_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    operation: Mapped[str] = mapped_column(String(16), primary_key=True)
    request_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    document_id: Mapped[str] = mapped_column(String(64), nullable=False)
    version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ProjectKnowledgeBaseModel(Base):
    __tablename__ = "project_knowledge_bases"
    __table_args__ = (
        # 项目仍为个人私用，保留 (project_id, owner_id) 复合外键。
        ForeignKeyConstraint(
            ["project_id", "owner_id"], ["projects.id", "projects.owner_id"], ondelete="CASCADE"
        ),
        # 拆除 (knowledge_base_id, owner_id) 复合外键，允许员工将有权限的公司/部门库绑定到个人项目。
        ForeignKeyConstraint(
            ["knowledge_base_id"],
            ["knowledge_bases.id"],
            name="fk_project_knowledge_bases_base",
            ondelete="CASCADE",
        ),
    )
    project_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    knowledge_base_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
