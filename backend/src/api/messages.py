"""会话消息只读 HTTP 接口。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Query, status

from api.converter.message import conversation_page_to_response
from api.dto.message import ConversationMessageListResponse
from api.security import CurrentAuth
from app.message import MessageService, SessionNotFoundError

SESSION_NOT_FOUND = "session_not_found"


def build_message_router(service: MessageService) -> APIRouter:
    """绑定消息查询用例并创建只读路由。"""
    router = APIRouter(prefix="/api/v1/sessions", tags=["messages"])

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
        try:
            page = await service.list_conversation(
                session_id,
                viewer_id=current_auth.user.id,
                before_sequence=before_sequence,
                limit=limit,
            )
        except SessionNotFoundError as error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=SESSION_NOT_FOUND,
            ) from error
        return conversation_page_to_response(page)

    return router


__all__ = ["build_message_router"]
