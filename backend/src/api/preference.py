"""个人工作偏好与显式记忆 HTTP REST 接口。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Response, status

from api.converter.preference import (
    memory_list_to_response,
    memory_to_response,
    profile_to_response,
)
from api.dto.preference import (
    CreateUserMemoryRequest,
    UpdateUserMemoryRequest,
    UpdateUserWorkProfileRequest,
    UserMemoryListResponse,
    UserMemoryResponse,
    UserWorkProfileResponse,
)
from api.security import CurrentAuth, verify_request_origin
from app.preference import PreferenceService
from domain.preference import (
    InvalidCustomInstructionsError,
    InvalidUserMemoryContentError,
    PreferenceError,
    UserMemoryLimitReachedError,
    UserMemoryNotFoundError,
    UserMemorySource,
)


def build_preference_router(service: PreferenceService) -> APIRouter:
    """绑定个人工作偏好与显式记忆服务，挂载于 /api/v1/me。"""
    router = APIRouter(
        prefix="/api/v1/me",
        tags=["preferences"],
        dependencies=[Depends(verify_request_origin)],
    )

    @router.get("/preferences", response_model=UserWorkProfileResponse)
    async def get_preferences(current_auth: CurrentAuth) -> UserWorkProfileResponse:
        """读取当前用户个人工作偏好（无记录时返回默认空指令与开启状态）。"""
        profile = await service.get_profile(current_auth.user.id)
        return profile_to_response(profile)

    @router.put("/preferences", response_model=UserWorkProfileResponse)
    async def update_preferences(
        request: UpdateUserWorkProfileRequest,
        current_auth: CurrentAuth,
    ) -> UserWorkProfileResponse:
        """更新当前用户个人工作背景指令与显式记忆开关。"""
        try:
            profile = await service.update_profile(
                current_auth.user.id,
                custom_instructions=request.custom_instructions,
                memory_enabled=request.memory_enabled,
            )
        except InvalidCustomInstructionsError as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=error.code,
            ) from error
        except PreferenceError as error:
            status_code = (
                status.HTTP_401_UNAUTHORIZED
                if error.code == "user_not_found"
                else status.HTTP_422_UNPROCESSABLE_CONTENT
            )
            raise HTTPException(
                status_code=status_code,
                detail=error.code,
            ) from error
        return profile_to_response(profile)

    @router.get("/memories", response_model=UserMemoryListResponse)
    async def list_memories(current_auth: CurrentAuth) -> UserMemoryListResponse:
        """按最近更新时间倒序返回当前用户全部显式记忆与配额上限。"""
        memory_list = await service.list_memories(current_auth.user.id)
        return memory_list_to_response(memory_list)

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
        try:
            memory = await service.create_memory(
                current_auth.user.id,
                request.content,
                source=UserMemorySource.MANUAL,
            )
        except InvalidUserMemoryContentError as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=error.code,
            ) from error
        except UserMemoryLimitReachedError as error:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=error.code,
            ) from error
        except PreferenceError as error:
            status_code = (
                status.HTTP_401_UNAUTHORIZED
                if error.code == "user_not_found"
                else status.HTTP_422_UNPROCESSABLE_CONTENT
            )
            raise HTTPException(
                status_code=status_code,
                detail=error.code,
            ) from error
        response.headers["Location"] = f"/api/v1/me/memories/{memory.id}"
        return memory_to_response(memory)

    @router.patch("/memories/{memory_id}", response_model=UserMemoryResponse)
    async def update_memory(
        memory_id: Annotated[str, Path(min_length=1, max_length=64)],
        request: UpdateUserMemoryRequest,
        current_auth: CurrentAuth,
    ) -> UserMemoryResponse:
        """修改当前用户指定显式记忆正文；跨用户或不存在返回 404。"""
        try:
            memory = await service.update_memory(
                current_auth.user.id,
                memory_id,
                content=request.content,
            )
        except InvalidUserMemoryContentError as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=error.code,
            ) from error
        except UserMemoryNotFoundError as error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=error.code,
            ) from error
        return memory_to_response(memory)

    @router.delete(
        "/memories/{memory_id}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    async def delete_memory(
        memory_id: Annotated[str, Path(min_length=1, max_length=64)],
        current_auth: CurrentAuth,
    ) -> Response:
        """删除当前用户指定显式记忆；跨用户或不存在返回 404。"""
        try:
            await service.delete_memory(current_auth.user.id, memory_id)
        except UserMemoryNotFoundError as error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=error.code,
            ) from error
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return router


__all__ = ["build_preference_router"]
