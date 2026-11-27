"""超级管理员专属组织树、用户角色分权与飞书通讯录同步 HTTP 接口。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, Query, Response

from api.converter.org import (
    department_list_to_response,
    feishu_sync_summary_to_response,
    org_user_page_to_response,
    org_user_to_response,
)
from api.dto.org import (
    DepartmentListResponse,
    FeishuSyncResponse,
    OrgUserListResponse,
    OrgUserResponse,
    UpdateUserRoleRequest,
    UpdateUserStatusRequest,
)
from api.errors import DomainErrorRoute
from api.security import AdminAuth
from app.org import OrgService
from domain.auth import UserRole, UserStatus
from domain.org import DeptAdminScopeInput


def build_org_admin_router(service: OrgService) -> APIRouter:
    """绑定超管组织与用户分权管理服务，挂载于 /api/v1/admin/org。"""
    router = APIRouter(
        prefix="/api/v1/admin/org",
        tags=["admin-org"],
        route_class=DomainErrorRoute,
    )

    @router.get("/departments", response_model=DepartmentListResponse)
    async def list_departments(
        admin_auth: AdminAuth,
        response: Response,
        include_archived: bool = False,
    ) -> DepartmentListResponse:
        """按 path、order_index 顺序返回组织部门树列表。"""
        departments = await service.list_departments(
            admin_auth,
            include_archived=include_archived,
        )
        response.headers["Cache-Control"] = "no-store"
        return department_list_to_response(departments)

    @router.get("/users", response_model=OrgUserListResponse)
    async def list_users(
        admin_auth: AdminAuth,
        response: Response,
        department_id: str | None = None,
        include_subdepartments: bool = True,
        role: UserRole | None = None,
        user_status: Annotated[UserStatus | None, Query(alias="status")] = None,
        q: str | None = None,
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> OrgUserListResponse:
        """分页检索企业用户，支持按部门子树、角色、状态与关键词筛选。"""
        result = await service.list_users(
            admin_auth,
            department_id=department_id,
            include_subdepartments=include_subdepartments,
            role=role,
            status=user_status,
            query=q,
            page=page,
            page_size=page_size,
        )
        response.headers["Cache-Control"] = "no-store"
        return org_user_page_to_response(result)

    @router.put("/users/{user_id}/role", response_model=OrgUserResponse)
    async def update_user_role(
        user_id: Annotated[str, Path(min_length=1, max_length=128)],
        request: UpdateUserRoleRequest,
        admin_auth: AdminAuth,
        response: Response,
    ) -> OrgUserResponse:
        """设置用户角色及部门管理范围，自动保护最后一位活跃超级管理员。"""
        scopes = tuple(
            DeptAdminScopeInput(
                department_id=item.department_id,
                include_subdepartments=item.include_subdepartments,
            )
            for item in request.managed_departments
        )
        updated = await service.update_user_role(
            admin_auth,
            user_id,
            role=request.role,
            managed_departments=scopes,
        )
        response.headers["Cache-Control"] = "no-store"
        return org_user_to_response(updated)

    @router.put("/users/{user_id}/status", response_model=OrgUserResponse)
    async def update_user_status(
        user_id: Annotated[str, Path(min_length=1, max_length=128)],
        request: UpdateUserStatusRequest,
        admin_auth: AdminAuth,
        response: Response,
    ) -> OrgUserResponse:
        """启用或停用用户；停用时同事务吊销其全部活跃会话并保护最后一位活跃超管。"""
        updated = await service.update_user_status(
            admin_auth,
            user_id,
            status=request.status,
        )
        response.headers["Cache-Control"] = "no-store"
        return org_user_to_response(updated)

    @router.post("/sync", response_model=FeishuSyncResponse)
    async def sync_feishu_org(
        admin_auth: AdminAuth,
        response: Response,
    ) -> FeishuSyncResponse:
        """手动触发飞书通讯录组织树同步；未启用飞书时返回 409 feishu_not_enabled。"""
        summary = await service.sync_feishu_org(admin_auth)
        response.headers["Cache-Control"] = "no-store"
        return feishu_sync_summary_to_response(summary)

    return router


__all__ = ["build_org_admin_router"]
