"""认证领域对象到应用结果模型转换。"""

from __future__ import annotations

from app.auth.model.result import UserResult
from domain.auth import User


def user_to_result(user: User) -> UserResult:
    """移除密码哈希和内部状态。"""
    return UserResult(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        created_at=user.created_at,
    )


__all__ = ["user_to_result"]
