"""会话目录 HTTP 接口。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response, status

from api.converter.session import session_list_to_response, session_to_response
from api.dto.session import (
    SessionCreateRequest,
    SessionListResponse,
    SessionResponse,
    SessionUpdateRequest,
)
from api.security import CurrentAuth, verify_request_origin
from app.session import (
    InvalidSessionFilterError,
    InvalidSessionTitleError,
    InvalidSessionUpdateError,
    ProjectNotFoundError,
    SessionNotFoundError,
    SessionProjectLockedError,
    SessionService,
)

SESSION_NOT_FOUND = "session_not_found"
SESSION_TITLE_INVALID = "session_title_invalid"
SESSION_UPDATE_INVALID = "session_update_invalid"
SESSION_FILTER_INVALID = "invalid_session_filter"
SESSION_PROJECT_LOCKED = "session_project_locked"
PROJECT_NOT_FOUND = "project_not_found"


def _session_not_found(error: Exception) -> HTTPException:
    """生成统一的不存在响应，避免泄漏跨用户资源。"""
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=SESSION_NOT_FOUND,
    )


def build_session_router(service: SessionService) -> APIRouter:
    """绑定会话用例并创建 CRUD 路由。"""
    router = APIRouter(
        prefix="/api/v1/sessions",
        tags=["sessions"],
        dependencies=[Depends(verify_request_origin)],
    )

    @router.post(
        "",
        response_model=SessionResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_session(
        request: SessionCreateRequest,
        response: Response,
        current_auth: CurrentAuth,
    ) -> SessionResponse:
        """创建可绑定项目或未绑定项目的会话。"""
        try:
            session = await service.create(
                current_auth.user.id,
                title=request.title,
                project_id=request.project_id,
            )
        except InvalidSessionTitleError as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=SESSION_TITLE_INVALID,
            ) from error
        except ProjectNotFoundError as error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=PROJECT_NOT_FOUND,
            ) from error
        response.headers["Location"] = f"/api/v1/sessions/{session.id}"
        return session_to_response(session)

    @router.get("", response_model=SessionListResponse)
    async def list_sessions(
        current_auth: CurrentAuth,
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 20,
        project_id: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
        unbound: bool = False,
    ) -> SessionListResponse:
        """分页查询当前用户的未删除会话。"""
        try:
            result = await service.list(
                current_auth.user.id,
                page=page,
                page_size=page_size,
                project_id=project_id,
                unbound=unbound,
            )
        except InvalidSessionFilterError as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=SESSION_FILTER_INVALID,
            ) from error
        return session_list_to_response(
            result,
            page_number=page,
            page_size=page_size,
        )

    @router.get("/{session_id}", response_model=SessionResponse)
    async def get_session(
        current_auth: CurrentAuth,
        session_id: Annotated[str, Path(min_length=1, max_length=64)],
    ) -> SessionResponse:
        """查询单个未删除会话。"""
        try:
            session = await service.get(current_auth.user.id, session_id)
        except SessionNotFoundError as error:
            raise _session_not_found(error) from error
        return session_to_response(session)

    @router.patch("/{session_id}", response_model=SessionResponse)
    async def update_session(
        current_auth: CurrentAuth,
        session_id: Annotated[str, Path(min_length=1, max_length=64)],
        request: SessionUpdateRequest,
    ) -> SessionResponse:
        """修改会话标题或首条消息前的项目绑定。"""
        supplied_fields = request.model_fields_set
        try:
            session = await service.update(
                current_auth.user.id,
                session_id,
                update_title="title" in supplied_fields,
                title=request.title,
                update_project="project_id" in supplied_fields,
                project_id=request.project_id,
            )
        except InvalidSessionUpdateError as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=SESSION_UPDATE_INVALID,
            ) from error
        except InvalidSessionTitleError as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=SESSION_TITLE_INVALID,
            ) from error
        except ProjectNotFoundError as error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=PROJECT_NOT_FOUND,
            ) from error
        except SessionProjectLockedError as error:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=SESSION_PROJECT_LOCKED,
            ) from error
        except SessionNotFoundError as error:
            raise _session_not_found(error) from error
        return session_to_response(session)

    @router.delete(
        "/{session_id}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    async def delete_session(
        current_auth: CurrentAuth,
        session_id: Annotated[str, Path(min_length=1, max_length=64)],
    ) -> Response:
        """软删除会话；同一用户重复删除保持幂等。"""
        try:
            await service.delete(current_auth.user.id, session_id)
        except SessionNotFoundError as error:
            raise _session_not_found(error) from error
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return router


__all__ = ["build_session_router"]
