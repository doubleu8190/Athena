"""认证应用结果到 HTTP DTO 转换。"""

from __future__ import annotations

from api.dto.auth import UserResponse
from app.auth import UserResult


def user_result_to_response(user: UserResult) -> UserResponse:
    """转换安全用户响应。"""
    return UserResponse(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        created_at=user.created_at,
    )


__all__ = ["user_result_to_response"]
