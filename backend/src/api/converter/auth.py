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
    return UserDepartmentItem.model_validate(item)


def user_result_to_response(user: UserResult) -> UserResponse:
    """转换安全用户响应（Pydantic 仅投影 DTO 显式声明字段，自动忽略敏感属性）。"""
    return UserResponse.model_validate(user)


def mock_users_to_response(items: tuple[OrgUserDetail, ...]) -> MockUserListResponse:
    """转换非生产 Mock 账号清单响应（需从 detail.user 展平用户字段）。"""
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
