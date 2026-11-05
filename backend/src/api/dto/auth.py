"""认证 HTTP 请求与响应 DTO。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class RegisterRequest(BaseModel):
    """开放注册请求。"""

    model_config = ConfigDict(extra="forbid")

    email: str
    password: str
    display_name: str | None = None


class LoginRequest(BaseModel):
    """邮箱密码登录请求。"""

    model_config = ConfigDict(extra="forbid")

    email: str
    password: str
    remember_me: bool = False


class ChangePasswordRequest(BaseModel):
    """已登录用户修改密码请求。"""

    model_config = ConfigDict(extra="forbid")

    current_password: str
    new_password: str


class UserResponse(BaseModel):
    """可安全返回给当前用户的账号字段。"""

    id: str
    email: str
    display_name: str | None
    created_at: datetime


__all__ = [
    "ChangePasswordRequest",
    "LoginRequest",
    "RegisterRequest",
    "UserResponse",
]
