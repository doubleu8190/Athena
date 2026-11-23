"""认证领域对象到应用结果模型转换。"""

from __future__ import annotations

from app.auth.model.result import UserResult
from domain.auth import User
from domain.org import UserDepartmentSummary


def user_to_result(
    user: User,
    *,
    departments: tuple[UserDepartmentSummary, ...] = (),
    managed_department_ids: tuple[str, ...] = (),
) -> UserResult:
    """移除密码哈希和内部状态，并附带可选部门归属与管辖部门 ID。"""
    return UserResult(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        created_at=user.created_at,
        role=user.role,
        auth_provider=user.auth_provider,
        employee_no=user.employee_no,
        job_title=user.job_title,
        avatar_url=user.avatar_url,
        departments=departments,
        managed_department_ids=managed_department_ids,
    )


__all__ = ["user_to_result"]
