"""运行执行和终态持久化用例。"""

from __future__ import annotations

from domain.events import (
    ApplicationEvent,
    EventDurability,
    EventPublisherPort,
    EventType,
)
from domain.runs import (
    CommandType,
    CommandStatus,
    CommandQueuePort,
    RunCommand,
    RunExecutorPort,
    RunLifecyclePort,
    RunStatus,
)


class RunExecutionService:
    """统一一次命令的执行、状态转换、事件和命令结果收敛。"""

    def __init__(
        self,
        lifecycle: RunLifecyclePort,
        commands: CommandQueuePort,
        executor: RunExecutorPort,
        events: EventPublisherPort,
    ) -> None:
        self._lifecycle = lifecycle
        self._commands = commands
        self._executor = executor
        self._events = events

    async def execute(self, command: RunCommand) -> None:
        if command.command_type.value == CommandType.RUN_CANCEL.value:
            await self._executor.cancel(command.run_id)
            if command.run_id:
                await self._lifecycle.cancel(command.run_id)
                await self._publish(command, EventType.RUN_CANCELLED)
            await self._commands.complete_command(
                command.command_id,
                status=CommandStatus.COMPLETED,
                result={"status": RunStatus.CANCELLED.value},
            )
            return
        if not command.run_id:
            await self._commands.complete_command(
                command.command_id,
                status=CommandStatus.FAILED,
                error={"code": "run_id_required"},
            )
            return
        await self._lifecycle.update_status(command.run_id, RunStatus.RUNNING)
        start_event = (
            EventType.RUN_RESUMED
            if command.command_type.value == CommandType.RUN_RESUME.value
            else EventType.RUN_STARTED
        )
        await self._publish(command, start_event)
        try:
            outcome = await self._executor.execute(command)
        except Exception as exc:
            await self.fail(
                command,
                {"code": "run_failed", "message": str(exc) or type(exc).__name__},
            )
            return
        if outcome.waiting:
            await self._publish(command, EventType.RUN_ROOT_SUSPENDED)
            return
        if outcome.error is not None:
            await self.fail(
                command,
                (
                    outcome.error
                    if isinstance(outcome.error, dict)
                    else {"message": str(outcome.error)}
                ),
            )
            return
        result = (
            dict(outcome.result)
            if isinstance(outcome.result, dict)
            else {"result": outcome.result}
        )
        await self._lifecycle.complete(command.run_id, result)
        await self._publish(command, EventType.RUN_COMPLETED)
        await self._commands.complete_command(
            command.command_id, status=CommandStatus.COMPLETED, result=result
        )

    async def fail(self, command: RunCommand, error: dict[str, object]) -> None:
        if command.run_id:
            await self._lifecycle.fail(command.run_id, error)
            await self._publish(command, EventType.RUN_FAILED, payload={"error": error})
        await self._commands.complete_command(
            command.command_id, status=CommandStatus.FAILED, error=error
        )

    async def _publish(
        self,
        command: RunCommand,
        event_type: EventType,
        *,
        payload: dict[str, object] | None = None,
    ) -> None:
        await self._events.publish(
            ApplicationEvent(
                event_type=event_type,
                durability=EventDurability.DURABLE,
                session_id=command.session_id,
                run_id=command.run_id,
                payload=payload or {"command_id": command.command_id},
            )
        )


__all__ = ["RunExecutionService"]
