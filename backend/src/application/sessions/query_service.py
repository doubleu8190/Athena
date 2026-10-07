"""Session 下属消息查询用例。"""

from __future__ import annotations

from domain.sessions import Message, MessageRepository, SessionRepository

from .service import SessionNotFoundError


class SessionMessageQueryService:
    """先确认会话存在，再通过消息 port 查询消息。"""

    def __init__(
        self,
        sessions: SessionRepository,
        messages: MessageRepository,
    ) -> None:
        self._sessions = sessions
        self._messages = messages

    async def list_messages(
        self, session_id: str, *, limit: int | None = None
    ) -> list[Message]:
        if await self._sessions.get(session_id) is None:
            raise SessionNotFoundError(session_id)
        return await self._messages.list_by_session(session_id, limit=limit)
