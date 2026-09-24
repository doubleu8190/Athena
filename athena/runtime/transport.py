"""供 SSE 订阅者使用的进程内实时事件传输。"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator

from athena.contracts.events import ApplicationEvent


class RuntimeEventPublisher:
    """为 Runtime 和 Gateway 适配器提供统一事件发布入口。"""

    def __init__(self, store) -> None:
        """创建基于持久化存储的事件发布器。

        参数:
            store (Any): 提供异步 ``publish`` 方法的事件存储。
        返回值:
            None。
        异常:
            不抛出业务异常；存储发布异常在调用 ``publish`` 时传播。
        """
        self.store = store

    async def publish(self, event: ApplicationEvent) -> ApplicationEvent:
        """发布应用事件并返回存储层确认后的事件。

        参数:
            event (ApplicationEvent): 要发布的事件，必须符合事件协议。
        返回值:
            ApplicationEvent: 存储层返回的事件，可能包含分配后的序号。
        异常:
            传播存储层写入或传输失败异常。
        """
        return await self.store.publish(event)

    async def publish_realtime(self, event: ApplicationEvent) -> ApplicationEvent:
        """广播不需要断线恢复的实时事件，不分配会话游标。"""
        await self.store.publish_realtime(event)
        return event


class SessionEventBus:
    """会话级事件总线的广播端。

    session_seq 的分配和持久化由 AgentStore 在同一会话锁内完成；本类只负责
    提交成功后的有界广播和订阅生命周期，避免传输层重复实现事件顺序。
    """

    def __init__(self) -> None:
        """初始化空的订阅集合。

        返回值:
            None。
        异常:
            不抛出业务异常。
        """
        self._subscribers: dict[str, set[asyncio.Queue[ApplicationEvent]]] = (
            defaultdict(set)
        )
        self._lock = asyncio.Lock()

    async def publish(self, event: ApplicationEvent) -> None:
        """将实时事件投递到目标会话的所有订阅队列。

        参数:
            event (ApplicationEvent): 要广播的事件。
        返回值:
            None: 事件已投递到各队列。
        异常:
            队列被取消或写入失败时传播相应的异步异常。
        """
        async with self._lock:
            queues = tuple(self._subscribers.get(event.session_id, ()))
        # 每个会话独立承担背压；队列满时只阻塞该会话的生产者，避免跨会话相互影响。
        for queue in queues:
            await queue.put(event)

    async def open_subscription(
        self, session_id: str
    ) -> asyncio.Queue[ApplicationEvent]:
        """为会话创建并登记一个有界订阅队列。

        参数:
            session_id (str): 要订阅的会话 ID。
        返回值:
            asyncio.Queue[ApplicationEvent]: 容量为 256 的事件队列。
        异常:
            底层锁被取消时传播 ``asyncio.CancelledError``。
        """
        queue: asyncio.Queue[ApplicationEvent] = asyncio.Queue(maxsize=256)
        async with self._lock:
            self._subscribers[session_id].add(queue)
        return queue

    async def close_subscription(
        self, session_id: str, queue: asyncio.Queue[ApplicationEvent]
    ) -> None:
        """移除一个会话订阅，并在无订阅时清理会话索引。

        参数:
            session_id (str): 订阅所属会话 ID。
            queue (asyncio.Queue): 要移除的订阅队列。
        返回值:
            None: 即使队列已移除或不存在也保持幂等。
        异常:
            底层锁被取消时传播 ``asyncio.CancelledError``。
        """
        async with self._lock:
            subscribers = self._subscribers.get(session_id)
            if subscribers:
                subscribers.discard(queue)
                if not subscribers:
                    self._subscribers.pop(session_id, None)

    async def subscribe(self, session_id: str) -> AsyncIterator[ApplicationEvent]:
        """持续生成指定会话的实时事件，并在迭代结束时自动退订。

        参数:
            session_id (str): 要订阅的会话 ID。
        返回值:
            AsyncIterator[ApplicationEvent]: 按投递顺序生成的事件迭代器。
        异常:
            消费者取消迭代时执行清理；底层队列异常会原样传播。
        """
        queue = await self.open_subscription(session_id)
        try:
            while True:
                yield await queue.get()
        finally:
            await self.close_subscription(session_id, queue)
