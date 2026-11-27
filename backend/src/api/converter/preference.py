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
    return UserWorkProfileResponse.model_validate(profile)


def memory_to_response(memory: UserMemory) -> UserMemoryResponse:
    """将单条显式记忆领域实体转换为 HTTP 响应。"""
    return UserMemoryResponse.model_validate(memory)


def memory_list_to_response(memory_list: UserMemoryList) -> UserMemoryListResponse:
    """将显式记忆列表及配额信息转换为 HTTP 响应。"""
    return UserMemoryListResponse.model_validate(memory_list)


__all__ = [
    "memory_list_to_response",
    "memory_to_response",
    "profile_to_response",
]
