"""Command 持久化完成后的进程内唤醒通知。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator


class CommandNotifier:
    """为单个 Runtime Consumer 提供轻量的 Command 到达通知。

    通知本身不携带 Command 内容，也不承担可靠投递职责；Command 始终以数据库记录为准。
    多次通知可以合并为一次唤醒，因为消费者每次唤醒后都会持续领取全部待处理命令。
    """

    def __init__(self) -> None:
        """创建空的 Command 通知器。

        返回值:
            None: 通知器已初始化。
        异常:
            不抛出业务异常。
        """
        self._notification = asyncio.Event()

    async def notify(self) -> None:
        """通知订阅者存在新的持久化 Command。

        参数:
            无；Command 必须已经在数据库事务中提交。
        返回值:
            None: 通知标记已设置。
        异常:
            不抛出业务异常。
        """
        self._notification.set()

    async def wait(self) -> None:
        """等待下一次 Command 到达通知。

        参数:
            无。
        返回值:
            None: 已收到至少一次通知；重复通知可能在一次等待中合并。
        异常:
            ``asyncio.CancelledError``: 当前等待任务被取消时传播。
        """
        await self._notification.wait()
        self._notification.clear()

    async def subscribe(self) -> AsyncIterator[None]:
        """持续订阅 Command 到达通知。

        参数:
            无。
        返回值:
            AsyncIterator[None]: 每次 Command 通知到达时生成一个空通知值。
        异常:
            ``asyncio.CancelledError``: 订阅任务被取消时传播。
        """
        while True:
            await self.wait()
            yield None
