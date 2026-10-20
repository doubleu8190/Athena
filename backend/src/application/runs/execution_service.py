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
        # Approval resumes are internal commands. Keep the original queued
        # command so its status can converge when the resumed execution ends.
        self._waiting_commands: dict[str, str] = {}

    async def execute(self, command: RunCommand) -> dict[str, object] | None:
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
            return {"status": RunStatus.CANCELLED.value}
        if not command.run_id:
            await self._commands.complete_command(
                command.command_id,
                status=CommandStatus.FAILED,
                error={"code": "run_id_required"},
            )
            return None
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
            self._waiting_commands[command.run_id] = command.command_id
            await self._publish(command, EventType.RUN_ROOT_SUSPENDED)
            return None
        if outcome.error is not None:
            await self.fail(
                command,
                (
                    outcome.error
                    if isinstance(outcome.error, dict)
                    else {"message": str(outcome.error)}
                ),
            )
            return None
        result = (
            dict(outcome.result)
            if isinstance(outcome.result, dict)
            else {"result": outcome.result}
        )
        await self._lifecycle.complete(command.run_id, result)
        await self._publish_result_message(command, result)
        await self._publish(command, EventType.RUN_COMPLETED)
        original_command_id = self._waiting_commands.pop(command.run_id, None)
        await self._commands.complete_command(
            command.command_id, status=CommandStatus.COMPLETED, result=result
        )
        if original_command_id and original_command_id != command.command_id:
            await self._commands.complete_command(
                original_command_id, status=CommandStatus.COMPLETED, result=result
            )
        converge = getattr(self._commands, "complete_active_command_for_run", None)
        if converge is not None:
            await converge(command.run_id, status=CommandStatus.COMPLETED, result=result)
        return result

    async def fail(self, command: RunCommand, error: dict[str, object]) -> None:
        if command.run_id:
            await self._lifecycle.fail(command.run_id, error)
            await self._publish(command, EventType.RUN_FAILED, payload={"error": error})
        await self._commands.complete_command(
            command.command_id, status=CommandStatus.FAILED, error=error
        )
        original_command_id = self._waiting_commands.pop(command.run_id, None) if command.run_id else None
        if original_command_id and original_command_id != command.command_id:
            await self._commands.complete_command(
                original_command_id, status=CommandStatus.FAILED, error=error
            )
        converge = getattr(self._commands, "complete_active_command_for_run", None)
        if converge is not None:
            await converge(command.run_id, status=CommandStatus.FAILED, error=error)

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

    async def _publish_result_message(
        self, command: RunCommand, result: dict[str, object]
    ) -> None:
        """Project a completed graph result into the session chat stream."""
        message_id = str(command.payload.get("message_id", ""))
        if not message_id or not command.run_id:
            return
        content = result.get("content")
        if not isinstance(content, str):
            nested = result.get("result")
            content = nested.get("content") if isinstance(nested, dict) else None
        if not isinstance(content, str) or not content:
            return
        stream_id = f"answer-{command.run_id}"
        await self._events.publish(
            ApplicationEvent(
                event_type=EventType.MESSAGE_DELTA,
                durability=EventDurability.DURABLE,
                session_id=command.session_id,
                run_id=command.run_id,
                message_id=message_id,
                stream_id=stream_id,
                stream_type="answer",
                chunk_id=1,
                payload={"delta": content},
            )
        )
        await self._events.publish(
            ApplicationEvent(
                event_type=EventType.MESSAGE_COMPLETED,
                durability=EventDurability.DURABLE,
                session_id=command.session_id,
                run_id=command.run_id,
                message_id=message_id,
                stream_id=stream_id,
                stream_type="answer",
                is_complete=True,
                payload={"content": content},
            )
        )


__all__ = ["RunExecutionService"]
