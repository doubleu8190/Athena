"""系统提示词领域版本到 HTTP DTO 的转换。"""

from __future__ import annotations

from api.dto.system_prompt import (
    SystemPromptCurrentResponse,
    SystemPromptVersionListResponse,
)
from domain.system_prompt import SystemPromptVersion, SystemPromptVersionPage


def system_prompt_to_response(
    value: SystemPromptVersion,
) -> SystemPromptCurrentResponse:
    """转换包含完整正文的版本投影。"""
    return SystemPromptCurrentResponse.model_validate(value)


def system_prompt_page_to_response(
    value: SystemPromptVersionPage,
) -> SystemPromptVersionListResponse:
    """转换不含正文的历史分页（自动忽略 summary DTO 未声明的正文字段）。"""
    return SystemPromptVersionListResponse.model_validate(value)


__all__ = ["system_prompt_page_to_response", "system_prompt_to_response"]
