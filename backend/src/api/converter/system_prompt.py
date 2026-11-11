"""系统提示词领域版本到 HTTP DTO 的转换。"""

from __future__ import annotations

from api.dto.system_prompt import (
    SystemPromptCurrentResponse,
    SystemPromptVersionListResponse,
    SystemPromptVersionSummary,
)
from domain.system_prompt import SystemPromptVersion, SystemPromptVersionPage


def system_prompt_to_response(
    value: SystemPromptVersion,
) -> SystemPromptCurrentResponse:
    """转换包含完整正文的版本投影。"""
    return SystemPromptCurrentResponse(
        id=value.id,
        kind=value.kind,
        content=value.content,
        created_by=value.created_by,
        created_at=value.created_at,
        restored_from_version_id=value.restored_from_version_id,
    )


def system_prompt_page_to_response(
    value: SystemPromptVersionPage,
) -> SystemPromptVersionListResponse:
    """转换不含正文的历史分页。"""
    return SystemPromptVersionListResponse(
        items=[
            SystemPromptVersionSummary(
                id=item.id,
                kind=item.kind,
                created_by=item.created_by,
                created_at=item.created_at,
                restored_from_version_id=item.restored_from_version_id,
            )
            for item in value.items
        ],
        page=value.page,
        page_size=value.page_size,
        total=value.total,
    )


__all__ = ["system_prompt_page_to_response", "system_prompt_to_response"]
