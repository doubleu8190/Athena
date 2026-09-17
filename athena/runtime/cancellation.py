"""运行中 Agent 任务的协作式取消注册表。"""

from __future__ import annotations

import asyncio


class CancellationRegistry:
    """按运行 ID 保存取消事件，并提供并发安全的访问方法。"""

    def __init__(self) -> None:
        """初始化空注册表。

        返回值:
            ``None``。
        异常:
            不抛出业务异常。
        """
        self._events: dict[str, asyncio.Event] = {}
        self._lock = asyncio.Lock()

    async def register(self, run_id: str) -> asyncio.Event:
        """注册运行任务并返回其取消事件。

        参数:
            run_id (str): 非空运行 ID；相同 ID 重复注册时复用原事件。
        返回值:
            asyncio.Event: 该运行对应的取消事件。
        异常:
            不抛出业务异常；底层锁被取消时传播 ``asyncio.CancelledError``。
        """
        async with self._lock:
            return self._events.setdefault(run_id, asyncio.Event())

    async def request_cancellation(self, run_id: str) -> None:
        """请求取消指定运行任务。

        参数:
            run_id (str): 非空运行 ID；尚未注册时会先创建事件。
        返回值:
            None: 取消请求已写入内存注册表。
        异常:
            底层锁被取消时传播 ``asyncio.CancelledError``。
        """
        async with self._lock:
            event = self._events.setdefault(run_id, asyncio.Event())
            event.set()

    async def unregister(self, run_id: str) -> None:
        """移除指定运行任务的取消事件。

        参数:
            run_id (str): 要移除的运行 ID；不存在时视为幂等成功。
        返回值:
            None: 注册表不再保留该运行的事件。
        异常:
            底层锁被取消时传播 ``asyncio.CancelledError``。
        """
        async with self._lock:
            self._events.pop(run_id, None)

    async def is_cancelled(self, run_id: str) -> bool:
        """读取运行任务是否已收到取消请求。

        参数:
            run_id (str): 要查询的运行 ID。
        返回值:
            bool: 已注册且事件已设置时返回 ``True``，否则返回 ``False``。
        异常:
            底层锁被取消时传播 ``asyncio.CancelledError``。
        """
        async with self._lock:
            event = self._events.get(run_id)
            return event.is_set() if event else False
