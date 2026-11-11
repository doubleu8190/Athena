"""系统提示词的类别、不可变版本和当前生效投影。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

MAX_SYSTEM_PROMPT_CONTENT_CHARACTERS = 100_000


class SystemPromptKind(StrEnum):
    """管理员可独立维护的两类系统提示词。"""

    MAIN = "main"
    COMPRESSION = "compression"


@dataclass(frozen=True, slots=True)
class SystemPromptVersion:
    """一条不可删除、不可修改的系统提示词正文版本。"""

    id: str
    kind: SystemPromptKind
    content: str
    created_by: str | None
    created_at: datetime
    restored_from_version_id: str | None


@dataclass(frozen=True, slots=True)
class SystemPromptVersionPage:
    """按创建时间稳定倒序读取的一页版本元数据。"""

    items: tuple[SystemPromptVersion, ...]
    total: int
    page: int
    page_size: int


@dataclass(frozen=True, slots=True)
class ResolvedRunSystemPrompts:
    """Run 创建事务内一次读取到的两类提示词版本。"""

    main: SystemPromptVersion
    compression: SystemPromptVersion


__all__ = [
    "MAX_SYSTEM_PROMPT_CONTENT_CHARACTERS",
    "ResolvedRunSystemPrompts",
    "SystemPromptKind",
    "SystemPromptVersion",
    "SystemPromptVersionPage",
]
