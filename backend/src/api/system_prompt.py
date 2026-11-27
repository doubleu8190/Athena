"""管理员系统提示词 HTTP 接口。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, Query

from api.converter.system_prompt import (
    system_prompt_page_to_response,
    system_prompt_to_response,
)
from api.dto.system_prompt import (
    SystemPromptCurrentResponse,
    SystemPromptRestoreRequest,
    SystemPromptSaveRequest,
    SystemPromptVersionListResponse,
    SystemPromptVersionResponse,
)
from api.errors import DomainErrorRoute
from api.security import AdminAuth
from app.system_prompt import SystemPromptService
from domain.system_prompt import SystemPromptKind

PromptKindPath = Annotated[SystemPromptKind, Path()]
VersionIdPath = Annotated[str, Path(min_length=1, max_length=128)]


def build_system_prompt_admin_router(service: SystemPromptService) -> APIRouter:
    """绑定管理员系统提示词用例并创建路由。"""
    router = APIRouter(
        prefix="/api/v1/admin/system-prompts",
        tags=["system-prompt-admin"],
        route_class=DomainErrorRoute,
    )

    @router.get("/{kind}", response_model=SystemPromptCurrentResponse)
    async def get_current(
        actor: AdminAuth,
        kind: PromptKindPath,
    ) -> SystemPromptCurrentResponse:
        """读取指定类别当前生效正文。"""
        value = await service.get_current(actor, kind)
        return system_prompt_to_response(value)

    @router.get(
        "/{kind}/versions",
        response_model=SystemPromptVersionListResponse,
    )
    async def list_versions(
        actor: AdminAuth,
        kind: PromptKindPath,
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> SystemPromptVersionListResponse:
        """分页读取指定类别历史版本元数据。"""
        value = await service.list_versions(
            actor,
            kind,
            page=page,
            page_size=page_size,
        )
        return system_prompt_page_to_response(value)

    @router.get(
        "/{kind}/versions/{version_id}",
        response_model=SystemPromptVersionResponse,
    )
    async def get_version(
        actor: AdminAuth,
        kind: PromptKindPath,
        version_id: VersionIdPath,
    ) -> SystemPromptVersionResponse:
        """读取同类别历史版本完整正文。"""
        value = await service.get_version(actor, kind, version_id)
        return SystemPromptVersionResponse.model_validate(value)

    @router.post("/{kind}/versions", response_model=SystemPromptCurrentResponse)
    async def save_version(
        request: SystemPromptSaveRequest,
        actor: AdminAuth,
        kind: PromptKindPath,
    ) -> SystemPromptCurrentResponse:
        """按当前版本基线保存并立即切换新正文。"""
        value = await service.save(
            actor,
            kind,
            content=request.content,
            expected_current_version_id=request.expected_current_version_id,
        )
        return system_prompt_to_response(value)

    @router.post("/{kind}/restore", response_model=SystemPromptCurrentResponse)
    async def restore_version(
        request: SystemPromptRestoreRequest,
        actor: AdminAuth,
        kind: PromptKindPath,
    ) -> SystemPromptCurrentResponse:
        """从同类历史正文生成新版本并立即切换。"""
        value = await service.restore(
            actor,
            kind,
            source_version_id=request.source_version_id,
            expected_current_version_id=request.expected_current_version_id,
        )
        return system_prompt_to_response(value)

    return router


__all__ = ["build_system_prompt_admin_router"]
