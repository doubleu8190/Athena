"""目标层命令消费者。"""

from __future__ import annotations

import asyncio

from application.runs.execution_service import RunExecutionService
from domain.runs import CommandQueuePort, RunCommand

class CommandConsumer:
    """只管理命令队列生命周期，执行细节委托给 RunExecutionService。"""

    def __init__(self, queue: CommandQueuePort, execution: RunExecutionService, *, poll_interval: float = 0.25) -> None:
        self._queue = queue
        self._execution = execution
        self._poll_interval = poll_interval
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._inflight: set[asyncio.Task[None]] = set()

    async def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self.run(), name="athena-command-consumer")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            await self._task
            self._task = None
        pending = tuple(self._inflight)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def run(self) -> None:
        while not self._stop.is_set():
            command = await self._queue.claim_next_command()
            if command is None:
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self._poll_interval)
                except asyncio.TimeoutError:
                    pass
                continue
            task = asyncio.create_task(
                self._execute_command(command),
                name=f"athena-command:{command.command_id}",
            )
            self._inflight.add(task)
            task.add_done_callback(self._inflight.discard)
            # Let the command task register active runs before claiming the
            # next command, which is significant for an immediately-following
            # cancellation command on an in-memory queue.
            await asyncio.sleep(0)

    async def _execute_command(self, command: RunCommand) -> None:
        try:
            await self._execution.execute(command)
        except asyncio.CancelledError:

            return
        except Exception as exc:
            await self._execution.fail(
                command,
                {"code": "command_failed", "message": str(exc) or type(exc).__name__},
            )


__all__ = ["CommandConsumer"]
