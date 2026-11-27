"""将会话领域模型转换为 HTTP DTO。"""

from __future__ import annotations

from api.dto.session import SessionListResponse, SessionResponse
from domain.session import Session, SessionPage


def session_to_response(session: Session) -> SessionResponse:
    """按 DTO 字段白名单投影单个会话响应，自动屏蔽 owner_id 与 deleted_time 等内部属性。"""
    return SessionResponse.model_validate(session)


def session_list_to_response(
    page: SessionPage,
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
