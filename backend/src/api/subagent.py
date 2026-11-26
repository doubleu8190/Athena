"""管理员 Subagent 配置管理 HTTP 路由。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Query, Response, status

from api.converter.subagent import (
    available_tools_to_response,
    create_request_to_command,
    subagent_page_to_response,
    subagent_to_response,
    update_request_to_command,
)
from api.dto.subagent import (
    SubagentAvailableToolsResponse,
    SubagentCreateRequest,
    SubagentEnabledRequest,
    SubagentListResponse,
    SubagentResponse,
    SubagentUpdateRequest,
)
from api.security import AdminAuth
from app.subagent import SubagentService
from domain.subagent import (
    SubagentAdminRequiredError,
    SubagentError,
    SubagentInvalidSessionError,
    SubagentModelUnavailableError,
    SubagentNotFoundError,
    SubagentSlugConflictError,
    SubagentSlugReservedError,
    SubagentToolForbiddenError,
    SubagentVersionConflictError,
)

SubagentIdPath = Annotated[str, Path(min_length=1, max_length=64)]


def build_subagent_admin_router(service: SubagentService) -> APIRouter:
    """绑定 Subagent 管理服务并创建管理员路由。"""
    router = APIRouter(
        prefix="/api/v1/admin/subagents",
        tags=["subagent-admin"],
    )

    @router.get("", response_model=SubagentListResponse)
    async def list_subagents(
        actor: AdminAuth,
        response: Response,
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        enabled: Annotated[bool | None, Query()] = None,
    ) -> SubagentListResponse:
        """分页列出 Subagent 配置，支持按启停状态过滤。"""
        response.headers["Cache-Control"] = "no-store"
        try:
            page = await service.list_subagents(
                actor,
                offset=offset,
                limit=limit,
                enabled=enabled,
            )
        except SubagentError as error:
            raise _subagent_error_to_http(error) from error
        return subagent_page_to_response(page)

    @router.get("/available-tools", response_model=SubagentAvailableToolsResponse)
    async def list_available_tools(
        actor: AdminAuth,
        response: Response,
    ) -> SubagentAvailableToolsResponse:
        """返回按来源分组的可选工具目录（不含嵌套调度工具）。"""
        response.headers["Cache-Control"] = "no-store"
        try:
            groups = await service.list_available_tools(actor)
        except SubagentError as error:
            raise _subagent_error_to_http(error) from error
        return available_tools_to_response(groups)

    @router.post(
        "",
        response_model=SubagentResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_subagent(
        request: SubagentCreateRequest,
        actor: AdminAuth,
        response: Response,
    ) -> SubagentResponse:
        """创建新的 Subagent 配置。"""
        response.headers["Cache-Control"] = "no-store"
        try:
            profile = await service.create_subagent(
                actor,
                create_request_to_command(request),
            )
        except SubagentError as error:
            raise _subagent_error_to_http(error) from error
        return subagent_to_response(profile)

    @router.get("/{subagent_id}", response_model=SubagentResponse)
    async def get_subagent(
        actor: AdminAuth,
        subagent_id: SubagentIdPath,
        response: Response,
    ) -> SubagentResponse:
        """按 ID 读取单个 Subagent 配置详情。"""
        response.headers["Cache-Control"] = "no-store"
        try:
            profile = await service.get_subagent(actor, subagent_id)
        except SubagentError as error:
            raise _subagent_error_to_http(error) from error
        return subagent_to_response(profile)

    @router.patch("/{subagent_id}", response_model=SubagentResponse)
    async def update_subagent(
        actor: AdminAuth,
        subagent_id: SubagentIdPath,
        request: SubagentUpdateRequest,
        response: Response,
    ) -> SubagentResponse:
        """按乐观锁版本号部分更新 Subagent 配置。"""
        response.headers["Cache-Control"] = "no-store"
        try:
            command = update_request_to_command(request)
            profile = await service.update_subagent(actor, subagent_id, command)
        except SubagentError as error:
            raise _subagent_error_to_http(error) from error
        return subagent_to_response(profile)

    @router.put("/{subagent_id}/enabled", response_model=SubagentResponse)
    async def set_subagent_enabled(
        actor: AdminAuth,
        subagent_id: SubagentIdPath,
        request: SubagentEnabledRequest,
        response: Response,
    ) -> SubagentResponse:
        """切换 Subagent 启停状态。"""
        response.headers["Cache-Control"] = "no-store"
        try:
            profile = await service.set_subagent_enabled(
                actor,
                subagent_id,
                request.enabled,
            )
        except SubagentError as error:
            raise _subagent_error_to_http(error) from error
        return subagent_to_response(profile)

    @router.delete(
        "/{subagent_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        response_class=Response,
    )
    async def delete_subagent(
        actor: AdminAuth,
        subagent_id: SubagentIdPath,
    ) -> Response:
        """幂等软删除指定 Subagent 配置。"""
        try:
            await service.delete_subagent(actor, subagent_id)
        except SubagentError as error:
            raise _subagent_error_to_http(error) from error
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return router


def _subagent_error_to_http(error: SubagentError) -> HTTPException:
    """将 Subagent 领域异常映射为约定的 HTTP 状态码与稳定错误码。"""
    if isinstance(error, SubagentInvalidSessionError):
        return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="not_authenticated")
    if isinstance(error, SubagentAdminRequiredError):
        return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin_required")
    if isinstance(error, SubagentNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=error.code)
    if isinstance(error, (SubagentSlugConflictError, SubagentVersionConflictError)):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=error.code)
    if isinstance(
        error,
        (
            SubagentSlugReservedError,
            SubagentToolForbiddenError,
            SubagentModelUnavailableError,
        ),
    ):
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=error.code,
        )
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail=error.code,
    )


__all__ = ["build_subagent_admin_router"]
