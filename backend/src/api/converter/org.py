"""组织领域对象到 HTTP DTO 转换。"""

from __future__ import annotations

from api.converter.auth import department_summary_to_dto
from api.dto.org import (
    DepartmentListResponse,
    DepartmentResponse,
    DeptAdminScopeResponse,
    FeishuSyncResponse,
    OrgUserListResponse,
    OrgUserResponse,
)
from domain.org import Department, FeishuSyncSummary, OrgUserDetail, OrgUserPage


def department_to_response(department: Department) -> DepartmentResponse:
    """转换单条部门响应。"""
    return DepartmentResponse(
        id=department.id,
        external_id=department.external_id,
        parent_id=department.parent_id,
        name=department.name,
        path=department.path,
        order_index=department.order_index,
        status=department.status,
        synced_at=department.synced_at,
        created_at=department.created_at,
        updated_at=department.updated_at,
    )


def department_list_to_response(departments: tuple[Department, ...]) -> DepartmentListResponse:
    """转换部门列表响应。"""
    return DepartmentListResponse(
        items=[department_to_response(item) for item in departments],
    )


def org_user_to_response(detail: OrgUserDetail) -> OrgUserResponse:
    """转换单个组织用户详情响应。"""
    user = detail.user
    return OrgUserResponse(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        role=user.role,
        status=user.status,
        auth_provider=user.auth_provider,
        employee_no=user.employee_no,
        job_title=user.job_title,
        avatar_url=user.avatar_url,
        last_synced_at=user.last_synced_at,
        created_at=user.created_at,
        updated_at=user.updated_at,
        departments=[department_summary_to_dto(item) for item in detail.departments],
        managed_departments=[
            DeptAdminScopeResponse(
                department_id=scope.department_id,
                department_name=scope.department_name,
                department_path=scope.department_path,
                include_subdepartments=scope.include_subdepartments,
            )
            for scope in detail.managed_departments
        ],
        managed_department_ids=list(detail.managed_department_ids),
    )


def org_user_page_to_response(page: OrgUserPage) -> OrgUserListResponse:
    """转换分页用户响应。"""
    return OrgUserListResponse(
        items=[org_user_to_response(item) for item in page.items],
        total=page.total,
        page=page.page,
        page_size=page.page_size,
    )


def feishu_sync_summary_to_response(summary: FeishuSyncSummary) -> FeishuSyncResponse:
    """转换飞书同步统计结果响应。"""
    return FeishuSyncResponse(
        departments_upserted=summary.departments_upserted,
        departments_archived=summary.departments_archived,
        users_upserted=summary.users_upserted,
        users_disabled=summary.users_disabled,
        sessions_revoked=summary.sessions_revoked,
        synced_at=summary.synced_at,
    )


__all__ = [
    "department_list_to_response",
    "department_to_response",
    "feishu_sync_summary_to_response",
    "org_user_page_to_response",
    "org_user_to_response",
]
