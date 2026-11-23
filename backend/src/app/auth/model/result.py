"""供 HTTP 层安全暴露的认证结果。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from domain.auth import AuthProvider, UserRole
from domain.org import UserDepartmentSummary


@dataclass(frozen=True, slots=True)
class UserResult:
    """不包含密码或认证会话内部状态的安全用户结果。"""

    id: str
    email: str | None
    display_name: str | None
    created_at: datetime
    role: UserRole
    auth_provider: AuthProvider = AuthProvider.LOCAL
    employee_no: str | None = None
    job_title: str | None = None
    avatar_url: str | None = None
    departments: tuple[UserDepartmentSummary, ...] = ()
    managed_department_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class IssuedAuthResult:
    """成功登录后返回的一次性 token 信息。"""

    user: UserResult
    token: str
    expires_at: datetime
    max_age_seconds: int


__all__ = ["IssuedAuthResult", "UserResult"]
