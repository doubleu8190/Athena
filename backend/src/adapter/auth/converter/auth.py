"""认证 ORM 行到领域模型转换。"""

from __future__ import annotations

from datetime import UTC, datetime

from adapter.auth.model.auth import AuthSessionModel, UserModel
from domain.auth import AuthSession, User, UserStatus


def _as_utc(value: datetime) -> datetime:
    """补齐 SQLite 等测试驱动丢失的 UTC 时区信息。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def user_to_domain(row: UserModel) -> User:
    """把用户 ORM 行转换为领域对象。"""
    return User(
        id=row.id,
        email=row.email,
        password_hash=row.password_hash,
        display_name=row.display_name,
        status=UserStatus(row.status),
        created_at=_as_utc(row.created_at),
        updated_at=_as_utc(row.updated_at),
        deleted_time=(_as_utc(row.deleted_time) if row.deleted_time is not None else None),
    )


def auth_session_to_domain(row: AuthSessionModel) -> AuthSession:
    """把登录会话 ORM 行转换为领域对象。"""
    return AuthSession(
        id=row.id,
        user_id=row.user_id,
        token_hash=row.token_hash,
        expires_at=_as_utc(row.expires_at),
        revoked_time=(_as_utc(row.revoked_time) if row.revoked_time is not None else None),
        created_at=_as_utc(row.created_at),
    )


__all__ = ["auth_session_to_domain", "user_to_domain"]
