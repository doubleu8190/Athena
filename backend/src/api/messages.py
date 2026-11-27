"""会话消息只读 HTTP 接口。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, Query

from api.converter.message import conversation_page_to_response
from api.dto.message import ConversationMessageListResponse
from api.errors import DomainErrorRoute
from api.security import CurrentAuth
from app.message import MessageService


def build_message_router(service: MessageService) -> APIRouter:
    """绑定消息查询用例并创建只读路由。"""
    router = APIRouter(
        prefix="/api/v1/sessions",
        tags=["messages"],
        route_class=DomainErrorRoute,
    )

    @router.get(
        "/{session_id}/messages",
        response_model=ConversationMessageListResponse,
    )
    async def list_messages(
        current_auth: CurrentAuth,
        session_id: Annotated[str, Path(min_length=1, max_length=64)],
        before_sequence: Annotated[int | None, Query(ge=1)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> ConversationMessageListResponse:
        """分页读取当前用户可见的会话消息。"""
        page = await service.list_conversation(
            session_id,
            viewer_id=current_auth.user.id,
            before_sequence=before_sequence,
            limit=limit,
        )
        return conversation_page_to_response(page)

    return router


__all__ = ["build_message_router"]
