"""应用服务共用的当前会话与角色校验守卫。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from domain.auth import AuthContext, CurrentSessionReader, User, UserRole


async def require_session_roles(
    auth_reader: CurrentSessionReader,
    actor: AuthContext,
    allowed_roles: frozenset[UserRole],
    now: datetime,
    *,
    invalid_session_error: Callable[[], Exception],
    forbidden_error: Callable[[], Exception],
) -> User:
    """复核当前登录会话有效性与允许的角色集合，按各域约定抛出 401/403 异常。"""
    user = await auth_reader.get_current_session_user(
        actor.user.id,
        actor.session_id,
        now,
    )
    if user is None:
        raise invalid_session_error()
    if user.role not in allowed_roles:
        raise forbidden_error()
    return user


__all__ = ["require_session_roles"]
