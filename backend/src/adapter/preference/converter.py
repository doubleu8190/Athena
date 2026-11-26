"""个人工作偏好与显式记忆 ORM 行到领域实体的转换。"""

from __future__ import annotations

from adapter.preference.model import UserMemoryModel, UserWorkProfileModel
from common.date import as_utc
from domain.preference import UserMemory, UserMemorySource, UserWorkProfile


def profile_to_domain(row: UserWorkProfileModel) -> UserWorkProfile:
    """将用户工作偏好 ORM 行转换为不可变领域实体。"""
    return UserWorkProfile(
        user_id=row.user_id,
        custom_instructions=row.custom_instructions,
        memory_enabled=row.memory_enabled,
        updated_at=as_utc(row.updated_at),
    )


def memory_to_domain(row: UserMemoryModel) -> UserMemory:
    """将显式记忆 ORM 行转换为不可变领域实体。"""
    return UserMemory(
        id=row.id,
        user_id=row.user_id,
        content=row.content,
        source=UserMemorySource(row.source),
        created_at=as_utc(row.created_at),
        updated_at=as_utc(row.updated_at),
    )


__all__ = ["memory_to_domain", "profile_to_domain"]
