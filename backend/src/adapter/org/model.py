"""企业组织部门树、用户归属与部门管理员分权范围 ORM 映射。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    false,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column

from adapter.base import Base


class DepartmentModel(Base):
    """企业部门表（物化路径 /root/.../dept_id/ 支持子树高效前缀检索）。"""

    __tablename__ = "departments"
    __table_args__ = (
        UniqueConstraint("external_id", name="uq_departments_external_id"),
        CheckConstraint(
            "status IN ('active', 'archived')",
            name="ck_departments_status",
        ),
        Index("ix_departments_path", "path"),
        # 在 PostgreSQL 非 C Locale 下支持物化路径 LIKE '/root/%' 前缀索引扫描。
        Index(
            "ix_departments_path_pattern",
            "path",
            postgresql_ops={"path": "varchar_pattern_ops"},
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    external_id: Mapped[str] = mapped_column(String(128), nullable=False)
    parent_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("departments.id", ondelete="SET NULL"),
        nullable=True,
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    path: Mapped[str] = mapped_column(String(2048), nullable=False)
    order_index: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="active",
        server_default="active",
    )
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class UserDepartmentModel(Base):
    """用户与部门多对多归属关系表。"""

    __tablename__ = "user_departments"
    __table_args__ = (Index("ix_user_departments_department", "department_id", "user_id"),)

    user_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    department_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("departments.id", ondelete="CASCADE"),
        primary_key=True,
    )
    is_primary: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=false(),
    )


class DeptAdminScopeModel(Base):
    """部门管理员分权管辖范围表。"""

    __tablename__ = "dept_admin_scopes"
    __table_args__ = (Index("ix_dept_admin_scopes_dept", "department_id"),)

    user_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    department_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("departments.id", ondelete="CASCADE"),
        primary_key=True,
    )
    include_subdepartments: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=true(),
    )
    granted_by: Mapped[str | None] = mapped_column(
        String(128),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


__all__ = ["DepartmentModel", "DeptAdminScopeModel", "UserDepartmentModel"]
