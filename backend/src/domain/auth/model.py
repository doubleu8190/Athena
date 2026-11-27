"""用户、认证方式、登录会话和已认证上下文。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class UserStatus(StrEnum):
    """用户可以处于的状态。"""

    ACTIVE = "active"
    DISABLED = "disabled"


class UserRole(StrEnum):
    """企业分权角色枚举：超级管理员、部门管理员与普通成员。"""

    SUPER_ADMIN = "super_admin"
    DEPT_ADMIN = "dept_admin"
    MEMBER = "member"


class AuthProvider(StrEnum):
    """用户身份认证来源：本地应急账号、飞书原生 SSO 或非生产 Mock 账号。"""

    LOCAL = "local"
    FEISHU = "feishu"
    MOCK = "mock"


@dataclass(frozen=True, slots=True)
class User:
    """平台用户及其身份来源、企业组织资料与可选本地密码凭据。"""

    id: str
    email: str | None
    password_hash: str | None
    display_name: str | None
    status: UserStatus
    created_at: datetime
    updated_at: datetime
    role: UserRole = UserRole.MEMBER
    auth_provider: AuthProvider = AuthProvider.LOCAL
    external_union_id: str | None = None
    external_open_id: str | None = None
    employee_no: str | None = None
    avatar_url: str | None = None
    job_title: str | None = None
    last_synced_at: datetime | None = None
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


@dataclass(frozen=True, slots=True)
class FeishuUserIdentity:
    """通过飞书 OAuth code 换取的脱敏用户身份信息。"""

    union_id: str | None
    open_id: str | None
    name: str | None
    email: str | None
    avatar_url: str | None
    employee_no: str | None
    job_title: str | None


__all__ = [
    "AuthContext",
    "AuthProvider",
    "AuthSession",
    "FeishuUserIdentity",
    "User",
    "UserRole",
    "UserStatus",
]
