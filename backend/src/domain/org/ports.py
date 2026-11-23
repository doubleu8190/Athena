"""组织架构持久化端口与飞书通讯录拉取端口。"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Protocol

from domain.auth import AuthContext, User, UserRole, UserStatus
from domain.org.model import (
    Department,
    DeptAdminScopeInput,
    FeishuSyncSummary,
    MockDepartmentSeed,
    MockDeptAdminScopeSeed,
    MockUserDepartmentSeed,
    OrgUserDetail,
    OrgUserPage,
    RemoteDepartment,
    RemoteEmployee,
    UserOrgMembership,
)


class OrgMembershipReader(Protocol):
    """认证接口所需的用户组织归属与管辖范围只读能力。"""

    async def get_user_org_membership(self, user_id: str) -> UserOrgMembership:
        """读取用户活跃部门列表及已授权管辖部门 ID 列表。"""

    async def list_active_mock_users(self) -> tuple[OrgUserDetail, ...]:
        """读取全部活跃的非生产 Mock 账号（含部门与管辖范围）。"""


class OrgRepository(OrgMembershipReader, Protocol):
    """组织部门、归属关系、分权范围及通讯录原子同步持久化端口。"""

    async def list_departments(
        self,
        *,
        include_archived: bool = False,
    ) -> tuple[Department, ...]:
        """按 path、order_index 排序返回部门列表。"""

    async def list_users(
        self,
        *,
        department_id: str | None = None,
        include_subdepartments: bool = True,
        role: UserRole | None = None,
        status: UserStatus | None = None,
        query: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> OrgUserPage:
        """按部门子树、角色、状态与关键词分页查询未删除用户。"""

    async def update_user_role(
        self,
        actor: AuthContext,
        user_id: str,
        *,
        role: UserRole,
        managed_departments: tuple[DeptAdminScopeInput, ...],
        now: datetime,
    ) -> OrgUserDetail:
        """在事务内复核超管身份并原子更新目标用户角色与部门管理范围。"""

    async def update_user_status(
        self,
        actor: AuthContext,
        user_id: str,
        *,
        status: UserStatus,
        now: datetime,
    ) -> OrgUserDetail:
        """在事务内复核超管身份并更新用户启停状态；停用时同事务撤销其全部会话。"""

    async def seed_mock_organization(
        self,
        *,
        departments: Sequence[MockDepartmentSeed],
        users: Sequence[User],
        user_departments: Sequence[MockUserDepartmentSeed],
        admin_scopes: Sequence[MockDeptAdminScopeSeed],
        now: datetime,
    ) -> None:
        """幂等预置非生产 Mock 组织树、测试账号及部门管理员范围。"""

    async def apply_feishu_sync(
        self,
        *,
        ordered_departments: Sequence[RemoteDepartment],
        active_employees: Sequence[RemoteEmployee],
        inactive_employees: Sequence[RemoteEmployee],
        now: datetime,
        department_id_factory: Callable[[], str],
        user_id_factory: Callable[[], str],
        actor: AuthContext | None = None,
    ) -> FeishuSyncSummary:
        """在单个数据库事务内原子提交部门树、在职员工归属与离职员工会话吊销。"""


class FeishuDirectoryClient(Protocol):
    """通讯录同步所需的飞书只读拉取端口。"""

    async def list_departments(self) -> list[RemoteDepartment]:
        """分页拉取全部可见部门列表。"""

    async def list_department_users(self, department_external_id: str) -> list[RemoteEmployee]:
        """分页拉取指定部门下直属员工列表。"""


__all__ = [
    "FeishuDirectoryClient",
    "OrgMembershipReader",
    "OrgRepository",
]
