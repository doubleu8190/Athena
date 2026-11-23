"""认证应用结果到 HTTP DTO 转换。"""

from __future__ import annotations

from api.dto.auth import (
    MockUserListResponse,
    MockUserResponse,
    UserDepartmentItem,
    UserResponse,
)
from app.auth import UserResult
from domain.org import OrgUserDetail, UserDepartmentSummary


def department_summary_to_dto(item: UserDepartmentSummary) -> UserDepartmentItem:
    """转换单条部门摘要投影。"""
    return UserDepartmentItem(
        id=item.id,
        name=item.name,
        path=item.path,
        is_primary=item.is_primary,
    )


def user_result_to_response(user: UserResult) -> UserResponse:
    """转换安全用户响应。"""
    return UserResponse(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        created_at=user.created_at,
        role=user.role,
        auth_provider=user.auth_provider,
        employee_no=user.employee_no,
        job_title=user.job_title,
        avatar_url=user.avatar_url,
        departments=[department_summary_to_dto(dept) for dept in user.departments],
        managed_department_ids=list(user.managed_department_ids),
    )


def mock_users_to_response(items: tuple[OrgUserDetail, ...]) -> MockUserListResponse:
    """转换非生产 Mock 账号清单响应。"""
    return MockUserListResponse(
        items=[
            MockUserResponse(
                id=detail.user.id,
                display_name=detail.user.display_name,
                email=detail.user.email,
                role=detail.user.role,
                employee_no=detail.user.employee_no,
                job_title=detail.user.job_title,
                avatar_url=detail.user.avatar_url,
                departments=[department_summary_to_dto(dept) for dept in detail.departments],
                managed_department_ids=list(detail.managed_department_ids),
            )
            for detail in items
        ]
    )


__all__ = [
    "department_summary_to_dto",
    "mock_users_to_response",
    "user_result_to_response",
]
