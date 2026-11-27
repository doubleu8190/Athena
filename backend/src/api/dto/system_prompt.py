"""管理员系统提示词接口 DTO。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from domain.system_prompt import (
    MAX_SYSTEM_PROMPT_CONTENT_CHARACTERS,
    SystemPromptKind,
)


class SystemPromptCurrentResponse(BaseModel):
    """当前正文、版本及操作来源。"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    kind: SystemPromptKind
    content: str
    created_by: str | None
    created_at: datetime
    restored_from_version_id: str | None


class SystemPromptVersionSummary(BaseModel):
    """历史列表中的版本元数据，不携带完整正文。"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    kind: SystemPromptKind
    created_by: str | None
    created_at: datetime
    restored_from_version_id: str | None


class SystemPromptVersionListResponse(BaseModel):
    """稳定倒序的版本分页。"""

    model_config = ConfigDict(from_attributes=True)

    items: list[SystemPromptVersionSummary]
    page: int
    page_size: int
    total: int


class SystemPromptVersionResponse(SystemPromptCurrentResponse):
    """指定历史版本的完整正文。"""


class SystemPromptSaveRequest(BaseModel):
    """以客户端读到的当前版本为 CAS 基线保存正文。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    content: str = Field(max_length=MAX_SYSTEM_PROMPT_CONTENT_CHARACTERS)
    expected_current_version_id: str = Field(min_length=1, max_length=128)


class SystemPromptRestoreRequest(BaseModel):
    """以同一 CAS 基线恢复同类别历史正文。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    source_version_id: str = Field(min_length=1, max_length=128)
    expected_current_version_id: str = Field(min_length=1, max_length=128)


__all__ = [
    "SystemPromptCurrentResponse",
    "SystemPromptRestoreRequest",
    "SystemPromptSaveRequest",
    "SystemPromptVersionListResponse",
    "SystemPromptVersionResponse",
    "SystemPromptVersionSummary",
]
