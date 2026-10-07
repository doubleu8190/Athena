"""目标层命令消费者。"""

from __future__ import annotations

import asyncio

from application.runs.execution_service import RunExecutionService
from domain.runs import CommandQueuePort


class CommandConsumer:
    """只管理命令队列生命周期，执行细节委托给 RunExecutionService。"""

    def __init__(self, queue: CommandQueuePort, execution: RunExecutionService, *, poll_interval: float = 0.25, notifier: object | None = None) -> None:
        self._queue = queue
        self._execution = execution
        self._poll_interval = poll_interval
        self._notifier = notifier
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self.run(), name="athena-command-consumer")

    async def stop(self) -> None:
        self._stop.set()
        if self._notifier is not None and hasattr(self._notifier, "notify"):
            await self._notifier.notify()
        if self._task is not None:
            await self._task
            self._task = None

    async def run(self) -> None:
        while not self._stop.is_set():
            command = await self._queue.claim_next_command()
            if command is None:
                if self._notifier is not None and hasattr(self._notifier, "wait"):
                    await self._notifier.wait()
                else:
                    try:
                        await asyncio.wait_for(self._stop.wait(), timeout=self._poll_interval)
                    except asyncio.TimeoutError:
                        pass
                continue
            try:
                await self._execution.execute(command)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._execution.fail(command, {"code": "command_failed", "message": str(exc) or type(exc).__name__})


__all__ = ["CommandConsumer"]
