"""管理员系统提示词 HTTP 接口。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Query

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
from api.security import AdminAuth
from app.system_prompt import SystemPromptService
from domain.system_prompt import (
    SystemPromptAuthorizationError,
    SystemPromptConflictError,
    SystemPromptError,
    SystemPromptKind,
    SystemPromptNotFoundError,
    SystemPromptUnavailableError,
)

PromptKindPath = Annotated[SystemPromptKind, Path()]
VersionIdPath = Annotated[str, Path(min_length=1, max_length=128)]


def build_system_prompt_admin_router(service: SystemPromptService) -> APIRouter:
    """绑定管理员系统提示词用例并创建路由。"""
    router = APIRouter(
        prefix="/api/v1/admin/system-prompts",
        tags=["system-prompt-admin"],
    )

    @router.get("/{kind}", response_model=SystemPromptCurrentResponse)
    async def get_current(
        actor: AdminAuth,
        kind: PromptKindPath,
    ) -> SystemPromptCurrentResponse:
        """读取指定类别当前生效正文。"""
        try:
            value = await service.get_current(actor, kind)
        except SystemPromptAuthorizationError as error:
            raise _authorization_error(error) from error
        except SystemPromptUnavailableError as error:
            raise HTTPException(503, error.code) from error
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
        try:
            value = await service.list_versions(
                actor,
                kind,
                page=page,
                page_size=page_size,
            )
        except SystemPromptAuthorizationError as error:
            raise _authorization_error(error) from error
        except SystemPromptError as error:
            raise HTTPException(422, error.code) from error
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
        try:
            value = await service.get_version(actor, kind, version_id)
        except SystemPromptAuthorizationError as error:
            raise _authorization_error(error) from error
        except SystemPromptNotFoundError as error:
            raise HTTPException(404, error.code) from error
        except SystemPromptError as error:
            raise HTTPException(422, error.code) from error
        return SystemPromptVersionResponse(**system_prompt_to_response(value).model_dump())

    @router.post("/{kind}/versions", response_model=SystemPromptCurrentResponse)
    async def save_version(
        request: SystemPromptSaveRequest,
        actor: AdminAuth,
        kind: PromptKindPath,
    ) -> SystemPromptCurrentResponse:
        """按当前版本基线保存并立即切换新正文。"""
        try:
            value = await service.save(
                actor,
                kind,
                content=request.content,
                expected_current_version_id=request.expected_current_version_id,
            )
        except SystemPromptAuthorizationError as error:
            raise _authorization_error(error) from error
        except SystemPromptConflictError as error:
            raise HTTPException(409, error.code) from error
        except SystemPromptUnavailableError as error:
            raise HTTPException(503, error.code) from error
        except SystemPromptError as error:
            raise HTTPException(422, error.code) from error
        return system_prompt_to_response(value)

    @router.post("/{kind}/restore", response_model=SystemPromptCurrentResponse)
    async def restore_version(
        request: SystemPromptRestoreRequest,
        actor: AdminAuth,
        kind: PromptKindPath,
    ) -> SystemPromptCurrentResponse:
        """从同类历史正文生成新版本并立即切换。"""
        try:
            value = await service.restore(
                actor,
                kind,
                source_version_id=request.source_version_id,
                expected_current_version_id=request.expected_current_version_id,
            )
        except SystemPromptAuthorizationError as error:
            raise _authorization_error(error) from error
        except SystemPromptConflictError as error:
            raise HTTPException(409, error.code) from error
        except SystemPromptNotFoundError as error:
            raise HTTPException(404, error.code) from error
        except SystemPromptUnavailableError as error:
            raise HTTPException(503, error.code) from error
        except SystemPromptError as error:
            raise HTTPException(422, error.code) from error
        return system_prompt_to_response(value)

    return router


def _authorization_error(error: SystemPromptAuthorizationError) -> HTTPException:
    """把最终身份复核失败映射为统一禁止响应。"""
    return HTTPException(status_code=403, detail=error.code)


__all__ = ["build_system_prompt_admin_router"]
