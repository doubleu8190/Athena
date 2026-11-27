"""Subagent HTTP DTO 与领域模型转换器。"""

from __future__ import annotations

from collections.abc import Sequence

from api.dto.subagent import (
    AvailableToolGroupResponse,
    SubagentAvailableToolsResponse,
    SubagentCreateRequest,
    SubagentListResponse,
    SubagentResponse,
    SubagentUpdateRequest,
)
from common.unset import UNSET
from domain.access import VisibilityScope
from domain.subagent import (
    AvailableToolGroup,
    CreateSubagentCommand,
    SubagentPage,
    SubagentProfile,
    SubagentValidationError,
    UpdateSubagentCommand,
)


def create_request_to_command(request: SubagentCreateRequest) -> CreateSubagentCommand:
    """将创建请求 DTO 转换为领域写命令。"""
    return CreateSubagentCommand(
        slug=request.slug,
        name=request.name,
        description=request.description,
        system_prompt=request.system_prompt,
        model_profile_id=request.model_profile_id,
        allowed_tool_ids=tuple(request.allowed_tool_ids),
        enabled=request.enabled,
        max_llm_requests=request.max_llm_requests,
        timeout_seconds=request.timeout_seconds,
        visibility_scope=VisibilityScope(request.visibility_scope),
        owner_department_id=request.owner_department_id,
        visibility_grants=(
            None
            if request.visibility_grants is None
            else tuple(item.to_domain() for item in request.visibility_grants)
        ),
    )


def update_request_to_command(request: SubagentUpdateRequest) -> UpdateSubagentCommand:
    """将部分更新请求 DTO 转换为带 UNSET 语义的领域写命令。"""
    fields = request.model_fields_set
    if "slug" in fields and request.slug is None:
        raise SubagentValidationError("subagent_slug_invalid")
    if "name" in fields and request.name is None:
        raise SubagentValidationError("subagent_name_invalid")
    if "description" in fields and request.description is None:
        raise SubagentValidationError("subagent_description_invalid")
    if "system_prompt" in fields and request.system_prompt is None:
        raise SubagentValidationError("subagent_system_prompt_invalid")
    if "allowed_tool_ids" in fields and request.allowed_tool_ids is None:
        raise SubagentValidationError("subagent_allowed_tools_invalid")
    if "visibility_scope" in fields and request.visibility_scope is None:
        raise SubagentValidationError("visibility_scope_invalid")

    return UpdateSubagentCommand(
        expected_version=request.version,
        slug=request.slug if "slug" in fields and request.slug is not None else UNSET,
        name=request.name if "name" in fields and request.name is not None else UNSET,
        description=(
            request.description
            if "description" in fields and request.description is not None
            else UNSET
        ),
        system_prompt=(
            request.system_prompt
            if "system_prompt" in fields and request.system_prompt is not None
            else UNSET
        ),
        model_profile_id=(request.model_profile_id if "model_profile_id" in fields else UNSET),
        allowed_tool_ids=(
            tuple(request.allowed_tool_ids)
            if "allowed_tool_ids" in fields and request.allowed_tool_ids is not None
            else UNSET
        ),
        max_llm_requests=(request.max_llm_requests if "max_llm_requests" in fields else UNSET),
        timeout_seconds=(request.timeout_seconds if "timeout_seconds" in fields else UNSET),
        visibility_scope=(
            VisibilityScope(request.visibility_scope)
            if "visibility_scope" in fields and request.visibility_scope is not None
            else UNSET
        ),
        owner_department_id=(
            request.owner_department_id if "owner_department_id" in fields else UNSET
        ),
        visibility_grants=(
            (
                None
                if request.visibility_grants is None
                else tuple(item.to_domain() for item in request.visibility_grants)
            )
            if "visibility_grants" in fields
            else UNSET
        ),
    )


def subagent_to_response(profile: SubagentProfile) -> SubagentResponse:
    """将领域 Subagent 实体转换为 HTTP 响应。"""
    return SubagentResponse.model_validate(profile)


def subagent_page_to_response(page: SubagentPage) -> SubagentListResponse:
    """将领域 Subagent 分页对象转换为 HTTP 列表响应。"""
    return SubagentListResponse.model_validate(page)


def available_tools_to_response(
    groups: Sequence[AvailableToolGroup],
) -> SubagentAvailableToolsResponse:
    """将分组工具目录转换为 HTTP 响应。"""
    return SubagentAvailableToolsResponse(
        groups=[AvailableToolGroupResponse.model_validate(group) for group in groups]
    )


__all__ = [
    "available_tools_to_response",
    "create_request_to_command",
    "subagent_page_to_response",
    "subagent_to_response",
    "update_request_to_command",
]
