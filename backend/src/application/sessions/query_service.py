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
        """保存对象依赖并初始化运行所需状态。

        参数：
        - `sessions`（SessionRepository）：读取会话身份和状态的仓储依赖；约束：无额外运行时限制。
        - `messages`（MessageRepository）：按顺序排列的模型对话消息；约束：无额外运行时限制。

        返回：
        - None：操作完成后不提供返回数据。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        self._sessions = sessions
        self._messages = messages

    async def list_messages(
        self, session_id: str, *, limit: int | None = None
    ) -> list[MessageResponse]:
        """按会话和数量上限读取消息列表。

        参数：
        - `session_id`（str）：目标会话的稳定标识，用于限定数据范围；约束：须通过方法内校验；失败条件见下方异常说明。
        - `limit`（int | None）：本次读取或处理的数量上限；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`。

        返回：
        - list[MessageResponse]：按时间顺序排列且不超过数量上限的会话消息列表。

        异常：
        - SessionNotFoundError：校验条件 `await self._sessions.get(session_id) is None` 成立时。
        - 未列出的依赖异常按当前实现传播。
        """
        if await self._sessions.get(session_id) is None:
            raise SessionNotFoundError(session_id)
        return [_to_response(value) for value in await self._messages.list_by_session(session_id, limit=limit)]


def _to_response(value: Message) -> MessageResponse:
    """将消息实体及嵌套值对象转换为应用响应。

    参数：
    - `value`（Message）：待解析、校验或转换的输入值；约束：无额外运行时限制。

    返回：
    - MessageResponse：供调用方展示或继续处理的 `MessageResponse` 响应对象。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
    """
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
