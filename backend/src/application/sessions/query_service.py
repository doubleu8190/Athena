"""Session 下属消息查询用例。"""

from __future__ import annotations

from domain.sessions import Message, MessageRepository, SessionRepository

from application.common.responses import AttachmentRefResponse, MessageResponse, ToolCallResponse

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
    ) -> list[MessageResponse]:
        if await self._sessions.get(session_id) is None:
            raise SessionNotFoundError(session_id)
        return [_to_response(value) for value in await self._messages.list_by_session(session_id, limit=limit)]


def _to_response(value: Message) -> MessageResponse:
    """将消息实体及嵌套值对象转换为应用响应。"""
    return MessageResponse(
        id=value.id,
        session_id=value.session_id,
        role=value.role.value,
        content=value.content,
        timestamp=value.timestamp,
        tool_calls=tuple(ToolCallResponse(item.id, item.name, dict(item.args)) for item in value.tool_calls),
        tool_call_id=value.tool_call_id,
        run_id=value.run_id,
        tool_call_record_id=value.tool_call_record_id,
        tool_name=value.tool_name,
        message_type=value.message_type,
        attachments=tuple(
            AttachmentRefResponse(item.id, item.filename, item.mime_type, item.size_bytes, item.status.value)
            for item in value.attachments
        ),
    )
