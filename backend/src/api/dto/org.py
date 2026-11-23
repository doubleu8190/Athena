"""超管组织架构与用户分权管理 HTTP DTO。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from api.dto.auth import UserDepartmentItem
from domain.auth import AuthProvider, UserRole, UserStatus
from domain.org import DepartmentStatus


class DepartmentResponse(BaseModel):
    """部门节点信息。"""

    id: str
    external_id: str
    parent_id: str | None
    name: str
    path: str
    order_index: int
    status: DepartmentStatus
    synced_at: datetime | None
    created_at: datetime
    updated_at: datetime


class DepartmentListResponse(BaseModel):
    """按物化路径与排序索引有序排列的部门列表。"""

    items: list[DepartmentResponse]


class DeptAdminScopeResponse(BaseModel):
    """部门管理员已授权管辖范围项。"""

    department_id: str
    department_name: str
    department_path: str
    include_subdepartments: bool


class OrgUserResponse(BaseModel):
    """超管视角下的企业用户完整投影（不含密码哈希）。"""

    id: str
    email: str | None
    display_name: str | None
    role: UserRole
    status: UserStatus
    auth_provider: AuthProvider
    employee_no: str | None
    job_title: str | None
    avatar_url: str | None
    last_synced_at: datetime | None
    created_at: datetime
    updated_at: datetime
    departments: list[UserDepartmentItem] = Field(default_factory=list)
    managed_departments: list[DeptAdminScopeResponse] = Field(default_factory=list)
    managed_department_ids: list[str] = Field(default_factory=list)


class OrgUserListResponse(BaseModel):
    """组织用户分页列表响应。"""

    items: list[OrgUserResponse]
    total: int
    page: int
    page_size: int


class ManagedDepartmentInput(BaseModel):
    """单条部门管辖范围配置输入。"""

    model_config = ConfigDict(extra="forbid")

    department_id: str = Field(min_length=1, max_length=64)
    include_subdepartments: bool = True


class UpdateUserRoleRequest(BaseModel):
    """更新用户角色及可选部门管辖范围请求。"""

    model_config = ConfigDict(extra="forbid")

    role: UserRole
    managed_departments: list[ManagedDepartmentInput] = Field(default_factory=list)


class UpdateUserStatusRequest(BaseModel):
    """更新用户启停状态请求。"""

    model_config = ConfigDict(extra="forbid")

    status: UserStatus


class FeishuSyncResponse(BaseModel):
    """手动触发飞书通讯录同步结果。"""

    departments_upserted: int
    departments_archived: int
    users_upserted: int
    users_disabled: int
    sessions_revoked: int
    synced_at: datetime


__all__ = [
    "DepartmentListResponse",
    "DepartmentResponse",
    "DeptAdminScopeResponse",
    "FeishuSyncResponse",
    "ManagedDepartmentInput",
    "OrgUserListResponse",
    "OrgUserResponse",
    "UpdateUserRoleRequest",
    "UpdateUserStatusRequest",
]
