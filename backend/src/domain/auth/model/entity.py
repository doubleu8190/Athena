"""用户、认证会话和已认证上下文。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class UserStatus(StrEnum):
    """用户可以处于的状态。"""

    ACTIVE = "active"
    DISABLED = "disabled"


@dataclass(frozen=True, slots=True)
class User:
    """平台用户及其密码凭据。"""

    id: str
    email: str
    password_hash: str
    display_name: str | None
    status: UserStatus
    created_at: datetime
    updated_at: datetime
    deleted_time: datetime | None = None


@dataclass(frozen=True, slots=True)
class AuthSession:
    """服务端可撤销的登录会话。"""

    id: str
    user_id: str
    token_hash: str
    expires_at: datetime
    revoked_time: datetime | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class AuthContext:
    """一次已通过服务端验证的登录上下文。"""

    user: User
    session_id: str


__all__ = ["AuthContext", "AuthSession", "User", "UserStatus"]
