"""统一三层资源可见性授权表 ORM 映射。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    String,
    UniqueConstraint,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column

from adapter.base import Base


class ResourceVisibilityGrantModel(Base):
    """记录 department 级资源面向部门或角色的显式可读授权。"""

    __tablename__ = "resource_visibility_grants"
    __table_args__ = (
        CheckConstraint(
            "resource_type IN ('knowledge_base', 'skill', 'mcp_server', 'subagent')",
            name="ck_resource_visibility_grants_resource_type",
        ),
        CheckConstraint(
            "target_type IN ('department', 'role')",
            name="ck_resource_visibility_grants_target_type",
        ),
        UniqueConstraint(
            "resource_type",
            "resource_id",
            "target_type",
            "target_id",
            name="uq_resource_visibility_grant",
        ),
        Index(
            "ix_resource_visibility_grants_lookup",
            "resource_type",
            "target_type",
            "target_id",
        ),
        Index(
            "ix_resource_visibility_grants_resource",
            "resource_type",
            "resource_id",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    resource_type: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(128), nullable=False)
    target_type: Mapped[str] = mapped_column(String(16), nullable=False)
    target_id: Mapped[str] = mapped_column(String(64), nullable=False)
    include_subdepartments: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=true(),
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


__all__ = ["ResourceVisibilityGrantModel"]
