"""会话文件元数据 ORM 映射。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from adapter.base import Base


class SessionFileModel(Base):
    """已发布的上传文件或工具产物；对象键只存在于服务端。"""

    __tablename__ = "session_files"
    __table_args__ = (
        CheckConstraint("source IN ('upload', 'artifact')", name="ck_session_files_source"),
        CheckConstraint("size_bytes >= 0", name="ck_session_files_size"),
        CheckConstraint(
            "(source = 'upload' AND source_call_id IS NULL AND output_index IS NULL) OR "
            "(source = 'artifact' AND source_call_id IS NOT NULL AND output_index >= 0)",
            name="ck_session_files_origin",
        ),
        CheckConstraint(
            "(source = 'upload' AND idempotency_key IS NOT NULL "
            "AND request_fingerprint IS NOT NULL) OR "
            "(source = 'artifact' AND idempotency_key IS NULL "
            "AND request_fingerprint IS NULL)",
            name="ck_session_files_idempotency",
        ),
        CheckConstraint(
            "extraction_status IN ('none', 'ready', 'failed')",
            name="ck_session_files_extraction_status",
        ),
        CheckConstraint(
            "extracted_text_bytes IS NULL OR extracted_text_bytes >= 0",
            name="ck_session_files_extracted_text_bytes",
        ),
        CheckConstraint(
            "(extraction_status = 'none' AND extracted_text_key IS NULL "
            "AND extracted_text_bytes IS NULL AND extraction_error_code IS NULL) OR "
            "(extraction_status = 'ready' AND extracted_text_key IS NOT NULL "
            "AND extracted_text_bytes IS NOT NULL AND extraction_error_code IS NULL) OR "
            "(extraction_status = 'failed' AND extracted_text_key IS NULL "
            "AND extracted_text_bytes IS NULL AND extraction_error_code IS NOT NULL)",
            name="ck_session_files_extraction_consistency",
        ),
        UniqueConstraint("session_id", "id", name="uq_session_files_session_id"),
        UniqueConstraint("object_key", name="uq_session_files_object_key"),
        UniqueConstraint(
            "source_call_id",
            "output_index",
            name="uq_session_files_artifact_origin",
        ),
        UniqueConstraint(
            "session_id",
            "owner_id",
            "source",
            "idempotency_key",
            name="uq_session_files_upload_request",
        ),
        ForeignKeyConstraint(
            ["session_id", "owner_id"],
            ["sessions.id", "sessions.owner_id"],
            name="fk_session_files_session_owner",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["source_call_id"],
            ["run_tool_calls.id"],
            name="fk_session_files_source_call",
            ondelete="RESTRICT",
        ),
        Index(
            "ix_session_files_session_created",
            "session_id",
            "created_at",
            "id",
        ),
        Index(
            "uq_session_files_extracted_text_key",
            "extracted_text_key",
            unique=True,
        ),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    owner_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    mime_type: Mapped[str] = mapped_column(String(255), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    object_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    source_call_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    output_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    request_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    extraction_status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="none",
        server_default="none",
    )
    extracted_text_key: Mapped[str | None] = mapped_column(
        String(1024),
        nullable=True,
        default=None,
    )
    extracted_text_bytes: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        default=None,
    )
    extraction_error_code: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        default=None,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


__all__ = ["SessionFileModel"]
