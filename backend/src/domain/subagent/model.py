"""Subagent 配置实体、写命令与领域校验规则。"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from domain.subagent.errors import (
    SubagentSlugReservedError,
    SubagentToolForbiddenError,
    SubagentValidationError,
)

DISPATCH_SUBAGENTS_TOOL_ID: Final[str] = "dispatch_subagents"
RESERVED_SUBAGENT_SLUGS: Final[frozenset[str]] = frozenset(
    {"main", "default", "system", "subagent"}
)
SUBAGENT_SLUG_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9][a-z0-9_-]{1,62}[a-z0-9]$")

MAX_SUBAGENT_NAME_LENGTH: Final[int] = 100
MAX_SUBAGENT_DESCRIPTION_LENGTH: Final[int] = 1000
MAX_SUBAGENT_SYSTEM_PROMPT_LENGTH: Final[int] = 32000
MAX_SUBAGENT_ALLOWED_TOOLS: Final[int] = 100
MIN_SUBAGENT_MAX_LLM_REQUESTS: Final[int] = 1
MAX_SUBAGENT_MAX_LLM_REQUESTS: Final[int] = 50
MIN_SUBAGENT_TIMEOUT_SECONDS: Final[int] = 10
MAX_SUBAGENT_TIMEOUT_SECONDS: Final[int] = 1800
MAX_SUBAGENT_MODEL_PROFILE_ID_LENGTH: Final[int] = 64
MAX_SUBAGENT_TOOL_ID_LENGTH: Final[int] = 128


class UnsetType:
    """用于区分 PATCH 请求中未传递字段与显式传递 None 的哨兵类型。"""

    __slots__ = ()


UNSET: Final[UnsetType] = UnsetType()


def _has_disallowed_control_chars(text: str, *, allow_whitespace_controls: bool) -> bool:
    """检查字符串是否包含不允许的控制字符。"""
    for ch in text:
        code = ord(ch)
        if code == 127 or (code < 32 and (not allow_whitespace_controls or ch not in "\n\r\t")):
            return True
    return False


def validate_subagent_slug(raw: str) -> str:
    """校验并规范化 Subagent slug，拦截格式错误与系统保留字。"""
    if not isinstance(raw, str):
        raise SubagentValidationError("subagent_slug_invalid")
    slug = raw.strip()
    if not SUBAGENT_SLUG_PATTERN.fullmatch(slug):
        raise SubagentValidationError("subagent_slug_invalid")
    if slug in RESERVED_SUBAGENT_SLUGS:
        raise SubagentSlugReservedError()
    return slug


def validate_subagent_name(raw: str) -> str:
    """校验 Subagent 名称：去首尾空白后 1..100 字符，禁止控制字符。"""
    if not isinstance(raw, str):
        raise SubagentValidationError("subagent_name_invalid")
    name = raw.strip()
    if (
        not name
        or len(name) > MAX_SUBAGENT_NAME_LENGTH
        or _has_disallowed_control_chars(name, allow_whitespace_controls=False)
    ):
        raise SubagentValidationError("subagent_name_invalid")
    return name


def validate_subagent_description(raw: str) -> str:
    """校验 Subagent 描述：去首尾空白后 1..1000 字符，禁止控制字符（允许换行/制表符）。"""
    if not isinstance(raw, str):
        raise SubagentValidationError("subagent_description_invalid")
    description = raw.strip()
    if (
        not description
        or len(description) > MAX_SUBAGENT_DESCRIPTION_LENGTH
        or _has_disallowed_control_chars(description, allow_whitespace_controls=True)
    ):
        raise SubagentValidationError("subagent_description_invalid")
    return description


def validate_subagent_system_prompt(raw: str) -> str:
    """校验 Subagent 系统提示词：去首尾空白后 1..32000 字符，允许换行与制表符。"""
    if not isinstance(raw, str):
        raise SubagentValidationError("subagent_system_prompt_invalid")
    prompt = raw.strip()
    if (
        not prompt
        or len(prompt) > MAX_SUBAGENT_SYSTEM_PROMPT_LENGTH
        or _has_disallowed_control_chars(prompt, allow_whitespace_controls=True)
    ):
        raise SubagentValidationError("subagent_system_prompt_invalid")
    return prompt


def validate_subagent_model_profile_id(raw: str | None) -> str | None:
    """校验绑定的模型配置 ID 格式。"""
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise SubagentValidationError("subagent_model_profile_id_invalid")
    profile_id = raw.strip()
    if (
        not profile_id
        or len(profile_id) > MAX_SUBAGENT_MODEL_PROFILE_ID_LENGTH
        or _has_disallowed_control_chars(profile_id, allow_whitespace_controls=False)
    ):
        raise SubagentValidationError("subagent_model_profile_id_invalid")
    return profile_id


def validate_subagent_allowed_tool_ids(raw: Sequence[str]) -> tuple[str, ...]:
    """校验并去重工具白名单，严禁包含 dispatch_subagents 且最多 100 个。"""
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
        raise SubagentValidationError("subagent_allowed_tools_invalid")
    deduped: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            raise SubagentValidationError("subagent_allowed_tools_invalid")
        tool_id = item.strip()
        if (
            not tool_id
            or len(tool_id) > MAX_SUBAGENT_TOOL_ID_LENGTH
            or _has_disallowed_control_chars(tool_id, allow_whitespace_controls=False)
        ):
            raise SubagentValidationError("subagent_allowed_tools_invalid")
        # 严禁子智能体白名单包含嵌套调度工具，防止递归派发。
        if tool_id == DISPATCH_SUBAGENTS_TOOL_ID:
            raise SubagentToolForbiddenError()
        if tool_id not in seen:
            seen.add(tool_id)
            deduped.append(tool_id)
    if len(deduped) > MAX_SUBAGENT_ALLOWED_TOOLS:
        raise SubagentValidationError("subagent_allowed_tools_too_many")
    return tuple(deduped)


def validate_subagent_max_llm_requests(raw: int | None) -> int | None:
    """校验单次子 Run 最大 LLM 请求次数（1..50）。"""
    if raw is None:
        return None
    if (
        type(raw) is not int
        or raw < MIN_SUBAGENT_MAX_LLM_REQUESTS
        or raw > MAX_SUBAGENT_MAX_LLM_REQUESTS
    ):
        raise SubagentValidationError("subagent_max_llm_requests_invalid")
    return raw


def validate_subagent_timeout_seconds(raw: int | None) -> int | None:
    """校验单次子 Run 超时秒数（10..1800）。"""
    if raw is None:
        return None
    if (
        type(raw) is not int
        or raw < MIN_SUBAGENT_TIMEOUT_SECONDS
        or raw > MAX_SUBAGENT_TIMEOUT_SECONDS
    ):
        raise SubagentValidationError("subagent_timeout_seconds_invalid")
    return raw


@dataclass(frozen=True, slots=True)
class SubagentProfile:
    """管理员配置的 Subagent 实体。"""

    id: str
    slug: str
    name: str
    description: str
    system_prompt: str
    model_profile_id: str | None
    allowed_tool_ids: tuple[str, ...]
    enabled: bool
    max_llm_requests: int | None
    timeout_seconds: int | None
    version: int
    created_by: str | None
    deleted_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class SubagentPage:
    """Subagent 分页列表投影。"""

    items: tuple[SubagentProfile, ...]
    total: int
    offset: int
    limit: int


@dataclass(frozen=True, slots=True)
class CreateSubagentCommand:
    """创建 Subagent 配置的写命令。"""

    slug: str
    name: str
    description: str
    system_prompt: str
    model_profile_id: str | None = None
    allowed_tool_ids: tuple[str, ...] = ()
    enabled: bool = True
    max_llm_requests: int | None = None
    timeout_seconds: int | None = None


@dataclass(frozen=True, slots=True)
class UpdateSubagentCommand:
    """部分更新 Subagent 配置的写命令（含乐观锁版本号）。"""

    expected_version: int
    slug: str | UnsetType = UNSET
    name: str | UnsetType = UNSET
    description: str | UnsetType = UNSET
    system_prompt: str | UnsetType = UNSET
    model_profile_id: str | None | UnsetType = UNSET
    allowed_tool_ids: tuple[str, ...] | UnsetType = UNSET
    max_llm_requests: int | None | UnsetType = UNSET
    timeout_seconds: int | None | UnsetType = UNSET


@dataclass(frozen=True, slots=True)
class SubagentUpdatePatch:
    """经领域校验后传递给仓储层的更新字段集合。"""

    slug: str
    name: str
    description: str
    system_prompt: str
    model_profile_id: str | None
    allowed_tool_ids: tuple[str, ...]
    max_llm_requests: int | None
    timeout_seconds: int | None
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class AvailableToolItem:
    """Subagent 可勾选的单个工具条目。"""

    tool_id: str
    name: str
    description: str
    source_type: str
    requires_approval: bool
    server_id: str | None = None
    server_name: str | None = None


@dataclass(frozen=True, slots=True)
class AvailableToolGroup:
    """按来源分组的 Subagent 可选工具目录。"""

    group_key: str
    label: str
    items: tuple[AvailableToolItem, ...]


__all__ = [
    "AvailableToolGroup",
    "AvailableToolItem",
    "CreateSubagentCommand",
    "DISPATCH_SUBAGENTS_TOOL_ID",
    "MAX_SUBAGENT_ALLOWED_TOOLS",
    "MAX_SUBAGENT_DESCRIPTION_LENGTH",
    "MAX_SUBAGENT_MAX_LLM_REQUESTS",
    "MAX_SUBAGENT_NAME_LENGTH",
    "MAX_SUBAGENT_SYSTEM_PROMPT_LENGTH",
    "MAX_SUBAGENT_TIMEOUT_SECONDS",
    "MIN_SUBAGENT_MAX_LLM_REQUESTS",
    "MIN_SUBAGENT_TIMEOUT_SECONDS",
    "RESERVED_SUBAGENT_SLUGS",
    "SUBAGENT_SLUG_PATTERN",
    "SubagentPage",
    "SubagentProfile",
    "SubagentUpdatePatch",
    "UNSET",
    "UnsetType",
    "UpdateSubagentCommand",
    "validate_subagent_allowed_tool_ids",
    "validate_subagent_description",
    "validate_subagent_max_llm_requests",
    "validate_subagent_model_profile_id",
    "validate_subagent_name",
    "validate_subagent_slug",
    "validate_subagent_system_prompt",
    "validate_subagent_timeout_seconds",
]
