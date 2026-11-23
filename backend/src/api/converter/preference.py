"""个人工作偏好与显式记忆领域实体到 HTTP 响应 DTO 的转换。"""

from __future__ import annotations

from api.dto.preference import (
    UserMemoryListResponse,
    UserMemoryResponse,
    UserWorkProfileResponse,
)
from domain.preference import UserMemory, UserMemoryList, UserWorkProfile


def profile_to_response(profile: UserWorkProfile) -> UserWorkProfileResponse:
    """将用户工作偏好领域实体转换为 HTTP 响应。"""
    return UserWorkProfileResponse(
        custom_instructions=profile.custom_instructions,
        memory_enabled=profile.memory_enabled,
        updated_at=profile.updated_at,
    )


def memory_to_response(memory: UserMemory) -> UserMemoryResponse:
    """将单条显式记忆领域实体转换为 HTTP 响应。"""
    return UserMemoryResponse(
        id=memory.id,
        content=memory.content,
        source=memory.source.value,
        created_at=memory.created_at,
        updated_at=memory.updated_at,
    )


def memory_list_to_response(memory_list: UserMemoryList) -> UserMemoryListResponse:
    """将显式记忆列表及配额信息转换为 HTTP 响应。"""
    return UserMemoryListResponse(
        items=[memory_to_response(item) for item in memory_list.items],
        total=memory_list.total,
        limit=memory_list.limit,
    )


__all__ = [
    "memory_list_to_response",
    "memory_to_response",
    "profile_to_response",
]
