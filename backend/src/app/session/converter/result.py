"""将会话领域模型转换为应用结果模型。"""

from __future__ import annotations

from app.session.model.result import SessionPageResult, SessionResult
from domain.session import Session, SessionPage


def session_to_result(session: Session) -> SessionResult:
    """隐藏领域内部所有者与软删除字段并生成应用结果。"""
    return SessionResult(
        id=session.id,
        title=session.title,
        project_id=session.project_id,
        created_at=session.created_at,
        updated_at=session.updated_at,
    )


def session_page_to_result(page: SessionPage) -> SessionPageResult:
    """批量转换会话分页结果。"""
    return SessionPageResult(
        items=tuple(session_to_result(session) for session in page.items),
        total=page.total,
    )


__all__ = ["session_page_to_result", "session_to_result"]
