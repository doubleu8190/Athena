"""个人工作偏好与显式记忆领域实体、常量与校验规则。"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from domain.preference.errors import (
    InvalidCustomInstructionsError,
    InvalidUserMemoryContentError,
)

MAX_CUSTOM_INSTRUCTIONS_LENGTH = 4000
MIN_USER_MEMORY_CONTENT_LENGTH = 1
MAX_USER_MEMORY_CONTENT_LENGTH = 500
MAX_USER_MEMORIES_PER_USER = 50
DEFAULT_PREFERENCE_MEMORY_TOKEN_BUDGET = 1500
SAVE_USER_MEMORY_TOOL_ID = "builtin_save_user_memory"


class UserMemorySource(StrEnum):
    """显式记忆的创建来源。"""

    MANUAL = "manual"
    CHAT_EXPLICIT = "chat_explicit"


def _has_forbidden_control_char(value: str) -> bool:
    """检查字符串是否包含除换行、回车与制表符以外的 Unicode Cc 控制字符。"""
    return any(
        unicodedata.category(character) == "Cc" and character not in ("\n", "\r", "\t")
        for character in value
    )


def validate_custom_instructions(value: str) -> str:
    """校验并规范化个人工作偏好指令：首尾去空白，长度 0..4000，禁止非法 Cc 控制字符。"""
    if not isinstance(value, str):
        raise InvalidCustomInstructionsError("custom_instructions_invalid")
    # 在 strip 之前先检查控制字符，防止 \\x0b/\\x0c 等被 strip 静默吞掉。
    if _has_forbidden_control_char(value):
        raise InvalidCustomInstructionsError("custom_instructions_invalid")
    normalized = value.strip()
    if len(normalized) > MAX_CUSTOM_INSTRUCTIONS_LENGTH:
        raise InvalidCustomInstructionsError("custom_instructions_invalid")
    return normalized


def validate_user_memory_content(value: str) -> str:
    """校验并规范化显式记忆正文：首尾去空白，长度 1..500，禁止非法 Cc 控制字符。"""
    if not isinstance(value, str):
        raise InvalidUserMemoryContentError("user_memory_content_invalid")
    # 在 strip 之前先检查控制字符，确保垂直制表符、换页符等非法控制字符被明确拒绝。
    if _has_forbidden_control_char(value):
        raise InvalidUserMemoryContentError("user_memory_content_invalid")
    normalized = value.strip()
    if not MIN_USER_MEMORY_CONTENT_LENGTH <= len(normalized) <= MAX_USER_MEMORY_CONTENT_LENGTH:
        raise InvalidUserMemoryContentError("user_memory_content_invalid")
    return normalized


@dataclass(frozen=True, slots=True)
class UserWorkProfile:
    """用户的个人工作偏好设置（工作背景指令与显式记忆开关）。"""

    user_id: str
    custom_instructions: str = ""
    memory_enabled: bool = True
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class UserMemory:
    """单条由用户手动或对话显式保存的轻量备忘记忆。"""

    id: str
    user_id: str
    content: str
    source: UserMemorySource
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class UserMemoryList:
    """当前用户全部显式记忆列表及配额信息。"""

    items: tuple[UserMemory, ...]
    total: int
    limit: int = MAX_USER_MEMORIES_PER_USER


@dataclass(frozen=True, slots=True)
class PreferenceContext:
    """供 Run 模型上下文注入的只读个人偏好快照。"""

    custom_instructions: str = ""
    memory_enabled: bool = True
    memories: tuple[UserMemory, ...] = ()


__all__ = [
    "DEFAULT_PREFERENCE_MEMORY_TOKEN_BUDGET",
    "MAX_CUSTOM_INSTRUCTIONS_LENGTH",
    "MAX_USER_MEMORIES_PER_USER",
    "MAX_USER_MEMORY_CONTENT_LENGTH",
    "MIN_USER_MEMORY_CONTENT_LENGTH",
    "SAVE_USER_MEMORY_TOOL_ID",
    "PreferenceContext",
    "UserMemory",
    "UserMemoryList",
    "UserMemorySource",
    "UserWorkProfile",
    "validate_custom_instructions",
    "validate_user_memory_content",
]
