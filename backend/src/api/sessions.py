"""会话目录 HTTP 接口。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, Query, Response, status

from api.converter.session import session_list_to_response, session_to_response
from api.dto.session import (
    SessionCreateRequest,
    SessionListResponse,
    SessionResponse,
    SessionUpdateRequest,
)
from api.errors import DomainErrorRoute
from api.security import CurrentAuth
from app.session import SessionService


def build_session_router(service: SessionService) -> APIRouter:
    """绑定会话用例并创建 CRUD 路由。"""
    router = APIRouter(
        prefix="/api/v1/sessions",
        tags=["sessions"],
        route_class=DomainErrorRoute,
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
        session = await service.create(
            current_auth.user.id,
            title=request.title,
            project_id=request.project_id,
        )
        response.headers["Location"] = f"/api/v1/sessions/{session.id}"
        return session_to_response(session)

    @router.get("", response_model=SessionListResponse)
    async def list_sessions(
        current_auth: CurrentAuth,
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 20,
        project_id: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
        unbound: bool = False,
        q: Annotated[str | None, Query(max_length=120)] = None,
    ) -> SessionListResponse:
        """分页查询当前用户的未删除会话，支持按标题关键字搜索。"""
        result = await service.list(
            current_auth.user.id,
            page=page,
            page_size=page_size,
            project_id=project_id,
            unbound=unbound,
            q=q,
        )
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
        session = await service.get(current_auth.user.id, session_id)
        return session_to_response(session)

    @router.patch("/{session_id}", response_model=SessionResponse)
    async def update_session(
        current_auth: CurrentAuth,
        session_id: Annotated[str, Path(min_length=1, max_length=64)],
        request: SessionUpdateRequest,
    ) -> SessionResponse:
        """修改会话标题或首条消息前的项目绑定。"""
        supplied_fields = request.model_fields_set
        session = await service.update(
            current_auth.user.id,
            session_id,
            update_title="title" in supplied_fields,
            title=request.title,
            update_project="project_id" in supplied_fields,
            project_id=request.project_id,
        )
        return session_to_response(session)

    @router.delete(
        "/{session_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        response_class=Response,
    )
    async def delete_session(
        current_auth: CurrentAuth,
        session_id: Annotated[str, Path(min_length=1, max_length=64)],
    ) -> Response:
        """软删除会话；同一用户重复删除保持幂等。"""
        await service.delete(current_auth.user.id, session_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return router


__all__ = ["build_session_router"]
