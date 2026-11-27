"""个人工作偏好与显式记忆 HTTP REST 接口。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, Response, status

from api.dto.preference import (
    CreateUserMemoryRequest,
    UpdateUserMemoryRequest,
    UpdateUserWorkProfileRequest,
    UserMemoryListResponse,
    UserMemoryResponse,
    UserWorkProfileResponse,
)
from api.errors import DomainErrorRoute
from api.security import CurrentAuth
from app.preference import PreferenceService
from domain.preference import UserMemorySource


def build_preference_router(service: PreferenceService) -> APIRouter:
    """绑定个人工作偏好与显式记忆服务，挂载于 /api/v1/me。"""
    router = APIRouter(
        prefix="/api/v1/me",
        tags=["preferences"],
        route_class=DomainErrorRoute,
    )

    @router.get("/preferences", response_model=UserWorkProfileResponse)
    async def get_preferences(current_auth: CurrentAuth) -> UserWorkProfileResponse:
        """读取当前用户个人工作偏好（无记录时返回默认空指令与开启状态）。"""
        profile = await service.get_profile(current_auth.user.id)
        return UserWorkProfileResponse.model_validate(profile)

    @router.put("/preferences", response_model=UserWorkProfileResponse)
    async def update_preferences(
        request: UpdateUserWorkProfileRequest,
        current_auth: CurrentAuth,
    ) -> UserWorkProfileResponse:
        """更新当前用户个人工作背景指令与显式记忆开关。"""
        profile = await service.update_profile(
            current_auth.user.id,
            custom_instructions=request.custom_instructions,
            memory_enabled=request.memory_enabled,
        )
        return UserWorkProfileResponse.model_validate(profile)

    @router.get("/memories", response_model=UserMemoryListResponse)
    async def list_memories(current_auth: CurrentAuth) -> UserMemoryListResponse:
        """按最近更新时间倒序返回当前用户全部显式记忆与配额上限。"""
        memory_list = await service.list_memories(current_auth.user.id)
        return UserMemoryListResponse.model_validate(memory_list)

    @router.post(
        "/memories",
        response_model=UserMemoryResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_memory(
        request: CreateUserMemoryRequest,
        response: Response,
        current_auth: CurrentAuth,
    ) -> UserMemoryResponse:
        """手动新增一条显式记忆（source=manual），超 50 条上限返回 409。"""
        memory = await service.create_memory(
            current_auth.user.id,
            request.content,
            source=UserMemorySource.MANUAL,
        )
        response.headers["Location"] = f"/api/v1/me/memories/{memory.id}"
        return UserMemoryResponse.model_validate(memory)

    @router.patch("/memories/{memory_id}", response_model=UserMemoryResponse)
    async def update_memory(
        memory_id: Annotated[str, Path(min_length=1, max_length=64)],
        request: UpdateUserMemoryRequest,
        current_auth: CurrentAuth,
    ) -> UserMemoryResponse:
        """修改当前用户指定显式记忆正文；跨用户或不存在返回 404。"""
        memory = await service.update_memory(
            current_auth.user.id,
            memory_id,
            content=request.content,
        )
        return UserMemoryResponse.model_validate(memory)

    @router.delete(
        "/memories/{memory_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        response_class=Response,
    )
    async def delete_memory(
        memory_id: Annotated[str, Path(min_length=1, max_length=64)],
        current_auth: CurrentAuth,
    ) -> Response:
        """删除当前用户指定显式记忆；跨用户或不存在返回 404。"""
        await service.delete_memory(current_auth.user.id, memory_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return router


__all__ = ["build_preference_router"]
