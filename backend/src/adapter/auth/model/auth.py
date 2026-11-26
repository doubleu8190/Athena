"""用户与登录会话 PostgreSQL ORM 映射。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from adapter.base import Base


class UserModel(Base):
    """平台用户表（支持本地应急超管、飞书原生 SSO 与非生产 Mock 账号）。"""

    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'disabled')",
            name="ck_users_status",
        ),
        CheckConstraint(
            "role IN ('super_admin', 'dept_admin', 'member')",
            name="ck_users_role",
        ),
        CheckConstraint(
            "email IS NULL OR email = lower(email)",
            name="ck_users_email_normalized",
        ),
        CheckConstraint(
            "auth_provider IN ('local', 'feishu', 'mock')",
            name="ck_users_auth_provider",
        ),
        CheckConstraint(
            "(auth_provider != 'local') OR (password_hash IS NOT NULL)",
            name="ck_users_local_password",
        ),
        Index(
            "uq_users_email_active",
            "email",
            unique=True,
            postgresql_where=text("email IS NOT NULL AND deleted_time IS NULL"),
        ),
        Index(
            "uq_users_external_union_id",
            "external_union_id",
            unique=True,
            postgresql_where=text("external_union_id IS NOT NULL AND deleted_time IS NULL"),
        ),
        Index(
            "uq_users_external_open_id",
            "external_open_id",
            unique=True,
            postgresql_where=text("external_open_id IS NOT NULL AND deleted_time IS NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(80), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    role: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="member",
        server_default="member",
    )
    auth_provider: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="local",
        server_default="local",
    )
    external_union_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    external_open_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    employee_no: Mapped[str | None] = mapped_column(String(64), nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    job_title: Mapped[str | None] = mapped_column(String(120), nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deleted_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )


class AuthSessionModel(Base):
    """服务端可撤销登录会话表。"""

    __tablename__ = "auth_sessions"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_auth_sessions_token_hash"),
        Index(
            "ix_auth_sessions_user_state",
            "user_id",
            "revoked_time",
            "expires_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


__all__ = ["AuthSessionModel", "UserModel"]
