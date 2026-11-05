"""供 HTTP 层安全暴露的认证结果。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class UserResult:
    """不包含密码或认证会话内部状态的安全用户结果。"""

    id: str
    email: str
    display_name: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class IssuedAuthResult:
    """成功登录或注册后返回的一次性 token 信息。"""

    user: UserResult
    token: str
    expires_at: datetime
    max_age_seconds: int


__all__ = ["IssuedAuthResult", "UserResult"]
