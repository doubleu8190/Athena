"""认证 HTTP 请求与响应 DTO。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from domain.auth import AuthProvider, UserRole


class LoginRequest(BaseModel):
    """本地应急账号邮箱密码登录请求。"""

    model_config = ConfigDict(extra="forbid")

    email: str
    password: str
    remember_me: bool = False


class ChangePasswordRequest(BaseModel):
    """已登录本地账号修改密码请求。"""

    model_config = ConfigDict(extra="forbid")

    current_password: str
    new_password: str


class UpdateCurrentUserRequest(BaseModel):
    """当前用户可编辑资料请求。"""

    model_config = ConfigDict(extra="forbid")

    display_name: str | None


class UserDepartmentItem(BaseModel):
    """用户所属部门摘要。"""

    id: str
    name: str
    path: str
    is_primary: bool


class UserResponse(BaseModel):
    """可安全返回给当前用户的账号与组织身份字段。"""

    id: str
    email: str | None
    display_name: str | None
    created_at: datetime
    role: UserRole
    auth_provider: AuthProvider = AuthProvider.LOCAL
    employee_no: str | None = None
    job_title: str | None = None
    avatar_url: str | None = None
    departments: list[UserDepartmentItem] = Field(default_factory=list)
    managed_department_ids: list[str] = Field(default_factory=list)


class AuthProvidersResponse(BaseModel):
    """当前部署启用的登录入口能力声明。"""

    feishu_enabled: bool
    local_login_enabled: bool = True
    mock_enabled: bool


class MockUserResponse(BaseModel):
    """非生产 Mock 快捷登录可选账号投影。"""

    id: str
    display_name: str | None
    email: str | None
    role: UserRole
    employee_no: str | None = None
    job_title: str | None = None
    avatar_url: str | None = None
    departments: list[UserDepartmentItem] = Field(default_factory=list)
    managed_department_ids: list[str] = Field(default_factory=list)


class MockUserListResponse(BaseModel):
    """非生产 Mock 账号清单响应。"""

    items: list[MockUserResponse]


class MockLoginRequest(BaseModel):
    """非生产 Mock 快捷登录请求。"""

    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(min_length=1, max_length=128)


__all__ = [
    "AuthProvidersResponse",
    "ChangePasswordRequest",
    "LoginRequest",
    "MockLoginRequest",
    "MockUserListResponse",
    "MockUserResponse",
    "UpdateCurrentUserRequest",
    "UserDepartmentItem",
    "UserResponse",
]
