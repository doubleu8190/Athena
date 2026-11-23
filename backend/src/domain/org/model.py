"""组织部门树、用户归属与部门管理员分权范围领域模型。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from domain.auth import User


class DepartmentStatus(StrEnum):
    """部门可用状态：活跃或远端已移除归档。"""

    ACTIVE = "active"
    ARCHIVED = "archived"


@dataclass(frozen=True, slots=True)
class Department:
    """企业通讯录部门节点（含规范物化路径 /root/.../id/）。"""

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


@dataclass(frozen=True, slots=True)
class UserDepartmentSummary:
    """用户所属部门的精简投影（供 /auth/me 与用户列表展示）。"""

    id: str
    name: str
    path: str
    is_primary: bool


@dataclass(frozen=True, slots=True)
class DeptAdminScopeInput:
    """设置部门管理员管辖范围时的单条部门输入。"""

    department_id: str
    include_subdepartments: bool = True


@dataclass(frozen=True, slots=True)
class DeptAdminScopeDetail:
    """部门管理员已授权管辖的单个部门范围详情。"""

    department_id: str
    department_name: str
    department_path: str
    include_subdepartments: bool


@dataclass(frozen=True, slots=True)
class UserOrgMembership:
    """当前用户的部门归属与管辖部门 ID 摘要。"""

    departments: tuple[UserDepartmentSummary, ...]
    managed_department_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class OrgUserDetail:
    """超管组织管理视角下的完整用户信息（不含密码哈希）。"""

    user: User
    departments: tuple[UserDepartmentSummary, ...]
    managed_departments: tuple[DeptAdminScopeDetail, ...]
    managed_department_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class OrgUserPage:
    """组织用户分页查询结果。"""

    items: tuple[OrgUserDetail, ...]
    total: int
    page: int
    page_size: int


@dataclass(frozen=True, slots=True)
class RemoteDepartment:
    """从外部通讯录（如飞书）拉取的原始部门记录。"""

    external_id: str
    parent_external_id: str | None
    name: str
    order_index: int = 0
    is_deleted: bool = False


@dataclass(frozen=True, slots=True)
class RemoteEmployee:
    """从外部通讯录（如飞书）拉取的原始员工记录。"""

    union_id: str | None
    open_id: str | None
    name: str | None
    email: str | None = None
    employee_no: str | None = None
    avatar_url: str | None = None
    job_title: str | None = None
    department_external_ids: tuple[str, ...] = ()
    is_active: bool = True


@dataclass(frozen=True, slots=True)
class MockDepartmentSeed:
    """非生产本地 Mock 组织树的单条部门种子定义。"""

    id: str
    external_id: str
    parent_id: str | None
    name: str
    path: str
    order_index: int


@dataclass(frozen=True, slots=True)
class MockUserDepartmentSeed:
    """非生产本地 Mock 用户的部门归属种子。"""

    user_id: str
    department_id: str
    is_primary: bool = True


@dataclass(frozen=True, slots=True)
class MockDeptAdminScopeSeed:
    """非生产本地 Mock 部门管理员管辖范围种子。"""

    user_id: str
    department_id: str
    include_subdepartments: bool = True


@dataclass(frozen=True, slots=True)
class FeishuSyncSummary:
    """一次飞书通讯录组织树原子同步的统计摘要。"""

    departments_upserted: int
    departments_archived: int
    users_upserted: int
    users_disabled: int
    sessions_revoked: int
    synced_at: datetime


__all__ = [
    "Department",
    "DepartmentStatus",
    "DeptAdminScopeDetail",
    "DeptAdminScopeInput",
    "FeishuSyncSummary",
    "MockDepartmentSeed",
    "MockDeptAdminScopeSeed",
    "MockUserDepartmentSeed",
    "OrgUserDetail",
    "OrgUserPage",
    "RemoteDepartment",
    "RemoteEmployee",
    "UserDepartmentSummary",
    "UserOrgMembership",
]
