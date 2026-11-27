"""超管组织树管理、用户角色分权、启停吊销与手动触发飞书同步用例。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING

from app.auth.guard import require_session_roles
from app.org.feishu_sync import FeishuOrgSyncService
from common.date import as_utc, utc_now
from domain.auth import AuthContext, CurrentSessionReader, UserRole, UserStatus
from domain.org import (
    Department,
    DeptAdminScopeInput,
    FeishuNotEnabledError,
    FeishuSyncSummary,
    OrgAuthorizationError,
    OrgUserDetail,
    OrgUserPage,
)

if TYPE_CHECKING:
    from adapter.org.repository import SqlAlchemyOrgRepository


class OrgService:
    """超级管理员专属组织架构与用户分权管理服务。"""

    def __init__(
        self,
        repository: SqlAlchemyOrgRepository,
        auth_reader: CurrentSessionReader,
        *,
        feishu_sync_service: FeishuOrgSyncService | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._repository = repository
        self._auth_reader = auth_reader
        self._feishu_sync_service = feishu_sync_service
        self._clock = clock

    async def list_departments(
        self,
        actor: AuthContext,
        *,
        include_archived: bool = False,
    ) -> tuple[Department, ...]:
        """复核超管会话后按物化路径与顺序返回部门列表。"""
        await self._authorize_super_admin(actor)
        return await self._repository.list_departments(include_archived=include_archived)

    async def list_users(
        self,
        actor: AuthContext,
        *,
        department_id: str | None = None,
        include_subdepartments: bool = True,
        role: UserRole | None = None,
        status: UserStatus | None = None,
        query: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> OrgUserPage:
        """复核超管会话后分页检索企业用户与所属部门、分权范围。"""
        await self._authorize_super_admin(actor)
        cleaned_dept = department_id.strip() if department_id and department_id.strip() else None
        cleaned_query = query.strip() if query and query.strip() else None
        return await self._repository.list_users(
            department_id=cleaned_dept,
            include_subdepartments=include_subdepartments,
            role=role,
            status=status,
            query=cleaned_query,
            page=page,
            page_size=page_size,
        )

    async def update_user_role(
        self,
        actor: AuthContext,
        user_id: str,
        *,
        role: UserRole,
        managed_departments: tuple[DeptAdminScopeInput, ...] = (),
    ) -> OrgUserDetail:
        """设置用户角色及部门管理范围，在写事务内锁定复核超管与最后超管保护。"""
        # 纯 DB 写路径直接由仓储写事务内 _require_super_admin_in_tx 复核，避免重复查库。
        return await self._repository.update_user_role(
            actor,
            user_id,
            role=role,
            managed_departments=managed_departments,
            now=as_utc(self._clock()),
        )

    async def update_user_status(
        self,
        actor: AuthContext,
        user_id: str,
        *,
        status: UserStatus,
    ) -> OrgUserDetail:
        """启用或停用用户；停用时同事务吊销其全部活跃会话并保护最后一位活跃超管。"""
        # 纯 DB 写路径直接由仓储写事务内 _require_super_admin_in_tx 复核，避免重复查库。
        return await self._repository.update_user_status(
            actor,
            user_id,
            status=status,
            now=as_utc(self._clock()),
        )

    async def sync_feishu_org(self, actor: AuthContext) -> FeishuSyncSummary:
        """手动触发飞书通讯录组织树同步；未启用飞书时抛 FeishuNotEnabledError。"""
        await self._authorize_super_admin(actor)
        if self._feishu_sync_service is None:
            raise FeishuNotEnabledError("feishu_not_enabled")
        return await self._feishu_sync_service.sync(actor=actor)

    async def _authorize_super_admin(self, actor: AuthContext) -> None:
        """每次进入服务操作前从数据库重新校验会话有效性与 super_admin 角色。"""
        await require_session_roles(
            self._auth_reader,
            actor,
            frozenset({UserRole.SUPER_ADMIN}),
            as_utc(self._clock()),
            invalid_session_error=lambda: OrgAuthorizationError("admin_required"),
            forbidden_error=lambda: OrgAuthorizationError("admin_required"),
        )


__all__ = ["OrgService"]
