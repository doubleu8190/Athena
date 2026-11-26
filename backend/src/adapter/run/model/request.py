"""Run 客户端命令幂等账本 ORM 映射。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKeyConstraint, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from adapter.base import Base, json_column_type


class RunRequestModel(Base):
    """保留操作幂等键、规范化内容指纹及结果资源引用。"""

    __tablename__ = "run_requests"
    __table_args__ = (
        UniqueConstraint(
            "owner_id",
            "session_id",
            "operation",
            "request_id",
            name="uq_run_requests_scope_key",
        ),
        ForeignKeyConstraint(
            ["session_id", "owner_id"],
            ["sessions.id", "sessions.owner_id"],
            name="fk_run_requests_session_owner",
            ondelete="CASCADE",
        ),
        Index("ix_run_requests_session_created", "session_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(128), nullable=False)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    operation: Mapped[str] = mapped_column(String(32), nullable=False)
    request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    result: Mapped[dict[str, object]] = mapped_column(json_column_type(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


__all__ = ["RunRequestModel"]
