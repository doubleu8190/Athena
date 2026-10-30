"""会话事件历史重放和实时订阅用例。"""

from __future__ import annotations

import asyncio

from domain.events import ApplicationEvent, EventPort
from domain.sessions import SessionRepository
from application.common.responses import EventResponse


class EventSessionNotFoundError(LookupError):
    """事件订阅目标会话不存在。"""


class EventStreamService:
    """组合会话权限、事件 watermark 和订阅生命周期。"""

    def __init__(
        self,
        sessions: SessionRepository,
        events: EventPort,
    ) -> None:
        """保存对象依赖并初始化运行所需状态。

        参数：
        - `sessions`（SessionRepository）：读取会话身份和状态的仓储依赖；约束：无额外运行时限制。
        - `events`（EventPort）：用于读取、写入或推送的事件存储依赖；约束：无额外运行时限制。

        返回：
        - None：操作完成后不提供返回数据。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        self._sessions = sessions
        self._events = events

    async def ensure_session(self, session_id: str) -> None:
        """确认会话。

        参数：
        - `session_id`（str）：目标会话的稳定标识，用于限定数据范围；约束：须通过方法内校验；失败条件见下方异常说明。

        返回：
        - None：操作完成后不提供返回数据。

        异常：
        - EventSessionNotFoundError：校验条件 `await self._sessions.get(session_id) is None` 成立时。
        - 未列出的依赖异常按当前实现传播。
        """
        if await self._sessions.get(session_id) is None:
            raise EventSessionNotFoundError(session_id)

    async def open_subscription(
        self, session_id: str
    ) -> tuple[asyncio.Queue[ApplicationEvent], int]:
        """为会话注册事件订阅，并返回队列和当前事件游标。

        参数：
        - `session_id`（str）：目标会话的稳定标识，用于限定数据范围；约束：无额外运行时限制。

        返回：
        - tuple[asyncio.Queue[ApplicationEvent], int]：事件队列及订阅建立时的最新事件游标。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        await self.ensure_session(session_id)
        return await self._events.open_subscription(session_id)

    async def replay(
        self, session_id: str, after: int, upto: int | None, limit: int = 256
    ) -> list[EventResponse]:
        """按游标和上界读取会话事件。

        参数：
        - `session_id`（str）：目标会话的稳定标识，用于限定数据范围；约束：无额外运行时限制。
        - `after`（int）：分页或事件回放的起始位置；具体边界语义由对应查询定义；约束：无额外运行时限制。
        - `upto`（int | None）：事件回放的结束游标边界；约束：允许为 None，按未提供值的分支处理。
        - `limit`（int）：本次读取或处理的数量上限；约束：可省略，默认值为 `256`。

        返回：
        - list[EventResponse]：符合会话、游标范围和数量上限的事件响应列表。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        return [
            _event_response(value)
            for value in await self._events.list_events_between(
                session_id, after, upto, limit=limit
            )
        ]

    @staticmethod
    def response(value: ApplicationEvent) -> EventResponse:
        """将应用事件转换为对外响应对象。

        参数：
        - `value`（ApplicationEvent）：待解析、校验或转换的输入值；约束：无额外运行时限制。

        返回：
        - EventResponse：供调用方展示或继续处理的 `EventResponse` 响应对象。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        return _event_response(value)

    async def close(
        self, session_id: str, queue: asyncio.Queue[ApplicationEvent]
    ) -> None:
        """取消会话事件订阅并移除对应队列。

        参数：
        - `session_id`（str）：目标会话的稳定标识，用于限定数据范围；约束：无额外运行时限制。
        - `queue`（asyncio.Queue[ApplicationEvent]）：保存待推送事件的队列；约束：无额外运行时限制。

        返回：
        - None：操作完成后不提供返回数据。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        await self._events.close_subscription(session_id, queue)

def _event_response(value: ApplicationEvent) -> EventResponse:
    """将事件信封转换为 SSE 使用的应用响应。

    参数：
    - `value`（ApplicationEvent）：待解析、校验或转换的输入值；约束：无额外运行时限制。

    返回：
    - EventResponse：供调用方展示或继续处理的 `EventResponse` 响应对象。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
    """
    return EventResponse(
        event_type=str(value.event_type), durability=str(value.durability), session_id=value.session_id,
        session_seq=value.session_seq, run_id=value.run_id, message_id=value.message_id,
        attachment_id=value.attachment_id, stream_id=value.stream_id, stream_type=value.stream_type,
        chunk_id=value.chunk_id, is_complete=value.is_complete, parent_run_id=value.parent_run_id,
        transition_id=value.transition_id, occurred_at=value.occurred_at, payload=dict(value.payload),
    )
