"""个人工作偏好与显式记忆 ORM 行到领域实体的转换。"""

from __future__ import annotations

from datetime import UTC, datetime

from adapter.preference.model import UserMemoryModel, UserWorkProfileModel
from domain.preference import UserMemory, UserMemorySource, UserWorkProfile


def _as_utc(value: datetime) -> datetime:
    """兼容 SQLite 测试驱动返回的无时区时间，统一转换为 UTC。"""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def profile_to_domain(row: UserWorkProfileModel) -> UserWorkProfile:
    """将用户工作偏好 ORM 行转换为不可变领域实体。"""
    return UserWorkProfile(
        user_id=row.user_id,
        custom_instructions=row.custom_instructions,
        memory_enabled=row.memory_enabled,
        updated_at=_as_utc(row.updated_at),
    )


def memory_to_domain(row: UserMemoryModel) -> UserMemory:
    """将显式记忆 ORM 行转换为不可变领域实体。"""
    return UserMemory(
        id=row.id,
        user_id=row.user_id,
        content=row.content,
        source=UserMemorySource(row.source),
        created_at=_as_utc(row.created_at),
        updated_at=_as_utc(row.updated_at),
    )


__all__ = ["memory_to_domain", "profile_to_domain"]
