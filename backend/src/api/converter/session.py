"""将会话应用结果转换为 HTTP DTO。"""

from __future__ import annotations

from api.dto.session import SessionListResponse, SessionResponse
from app.session.model.result import SessionPageResult, SessionResult


def session_to_response(session: SessionResult) -> SessionResponse:
    """转换单个会话响应。"""
    return SessionResponse(
        id=session.id,
        title=session.title,
        project_id=session.project_id,
        created_at=session.created_at,
        updated_at=session.updated_at,
    )


def session_list_to_response(
    page: SessionPageResult,
    *,
    page_number: int,
    page_size: int,
) -> SessionListResponse:
    """转换会话分页响应。"""
    return SessionListResponse(
        items=[session_to_response(session) for session in page.items],
        total=page.total,
        page=page_number,
        page_size=page_size,
    )


__all__ = ["session_list_to_response", "session_to_response"]
