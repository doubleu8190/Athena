"""运行执行和终态持久化用例。"""

from __future__ import annotations

from datetime import datetime, timezone
from time import perf_counter

from domain.events import (
    ApplicationEvent,
    EventDurability,
    EventPort,
    EventType,
)
from domain.memory import MemoryJobPort
from domain.runs import (
    CommandType,
    CommandStatus,
    CommandQueuePort,
    RunCommand,
    RunCancellationResult,
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
        events: EventPort,
        memory_jobs: MemoryJobPort | None = None,
    ) -> None:
        self._lifecycle = lifecycle
        self._commands = commands
        self._executor = executor
        self._events = events
        self._memory_jobs = memory_jobs
        # Approval resumes are internal commands. Keep the original queued
        # command so its status can converge when the resumed execution ends.
        self._waiting_commands: dict[str, str] = {}
        self._run_started: dict[str, tuple[datetime, float]] = {}

    async def execute(self, command: RunCommand) -> dict[str, object] | None:
        if command.command_type.value == CommandType.RUN_CANCEL.value:
            cancellation: RunCancellationResult | None = None
            if command.run_id:
                cancellation = await self._lifecycle.cancel(
                    command.run_id,
                    reason=str(command.payload.get("reason", "USER_REQUEST")),
                )
            # 先持久化取消栅栏，再中断 Root，避免并发的 SubmitPlan 或 Worker 领取穿过取消边界。
            cancel_error: Exception | None = None
            try:
                await self._executor.cancel(command.run_id)
            except Exception as exc:
                cancel_error = exc
            if (
                command.run_id
                and cancellation is not None
                and cancellation.changed
                and not getattr(
                    self._lifecycle, "writes_cancellation_events_to_outbox", False
                )
            ):
                status = str(cancellation.status)
                event_type = (
                    EventType.RUN_CANCELLING
                    if status == RunStatus.CANCELLING.value
                    else EventType.RUN_CANCELLED
                )
                await self._publish(
                    command,
                    event_type,
                    payload={
                        **self._run_payload(command, status),
                        "external_effect_status": cancellation.external_effect_status,
                    },
                )
            if cancel_error is not None:
                raise cancel_error
            await self._commands.complete_command(
                command.command_id,
                status=CommandStatus.COMPLETED,
                result={
                    "status": (
                        str(cancellation.status)
                        if cancellation is not None
                        else "unchanged"
                    ),
                    "external_effect_status": (
                        cancellation.external_effect_status
                        if cancellation is not None
                        else "unknown"
                    ),
                },
            )
            return {
                "status": (
                    str(cancellation.status)
                    if cancellation is not None
                    else "unchanged"
                ),
                "external_effect_status": (
                    cancellation.external_effect_status
                    if cancellation is not None
                    else "unknown"
                ),
            }
        if not command.run_id:
            await self._commands.complete_command(
                command.command_id,
                status=CommandStatus.FAILED,
                error={"code": "run_id_required"},
            )
            return None
        started = await self._lifecycle.update_status(
            command.run_id, RunStatus.RUNNING
        )
        if not started:
            result = {"status": RunStatus.CANCELLED.value}
            await self._commands.complete_command(
                command.command_id,
                status=CommandStatus.CANCELLED,
                result=result,
            )
            return result
        if command.run_id not in self._run_started:
            self._run_started[command.run_id] = (datetime.now(timezone.utc), perf_counter())
        start_event = (
            EventType.RUN_RESUMED
            if command.command_type.value == CommandType.RUN_RESUME.value
            else EventType.RUN_STARTED
        )
        await self._publish(
            command,
            start_event,
            payload=self._run_payload(command, "running", started_at=self._run_started[command.run_id][0]),
        )
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
            await self._arm_root_wait(command.run_id, outcome.result)
            await self._publish(command, EventType.RUN_ROOT_SUSPENDED, payload=self._run_payload(command, "waiting"))
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
        if not getattr(
            self._lifecycle, "writes_completion_artifacts_to_outbox", False
        ):
            await self._publish_result_message(command, result)
            await self._enqueue_memory(command, result)
            await self._publish(
                command,
                EventType.RUN_COMPLETED,
                payload=self._run_payload(
                    command, "completed", summary="运行已完成"
                ),
            )
        self._run_started.pop(command.run_id, None)
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

    async def _arm_root_wait(self, run_id: str, result: object) -> None:
        """Persist the exact Round interrupt before a resume worker can claim it."""
        arm = getattr(self._lifecycle, "arm_root_wait", None)
        if not callable(arm) or not isinstance(result, dict):
            return
        interrupts = result.get("__interrupt__", ())
        if not isinstance(interrupts, (list, tuple)) or len(interrupts) != 1:
            return
        interrupt = interrupts[0]
        value = getattr(interrupt, "value", None)
        checkpoint_id = result.get("__root_checkpoint_id__")
        if (
            not isinstance(value, dict)
            or value.get("type") != "round_wait"
            or not checkpoint_id
        ):
            return
        await arm(
            run_id,
            plan_id=str(value.get("plan_id", "")),
            round_id=str(value.get("round_id", "")),
            plan_version=int(value.get("plan_version", 0)),
            interrupt_id=str(getattr(interrupt, "id", "")),
            checkpoint_id=str(checkpoint_id),
        )

    async def fail(self, command: RunCommand, error: dict[str, object]) -> None:
        if command.run_id:
            await self._lifecycle.fail(command.run_id, error)
            await self._publish(command, EventType.RUN_FAILED, payload=self._run_payload(command, "failed", error=error))
            self._run_started.pop(command.run_id, None)
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

    def _run_payload(
        self,
        command: RunCommand,
        status: str,
        *,
        started_at: datetime | None = None,
        summary: str | None = None,
        error: dict[str, object] | None = None,
    ) -> dict[str, object]:
        run_id = command.run_id or ""
        payload: dict[str, object] = {
            "command_id": command.command_id,
            "activity_id": f"run:{run_id}",
            "activity_type": "run",
            "name": "run",
            "status": status,
        }
        state = self._run_started.get(run_id)
        start_time = started_at or (state[0] if state else None)
        if start_time is not None:
            payload["started_at"] = start_time.isoformat()
        if status in {"completed", "failed", "cancelled"} and state:
            payload["duration_ms"] = int((perf_counter() - state[1]) * 1000)
            payload["completed_at"] = datetime.now(timezone.utc).isoformat()
        if summary:
            payload["summary"] = summary
        if error:
            safe_error: dict[str, object] = {
                key: value for key, value in error.items()
                if key in {"code", "stage", "retryable"}
            }
            safe_error["message"] = str(error.get("code", "run_failed"))
            payload["error"] = safe_error
        return payload

    async def _enqueue_memory(
        self, command: RunCommand, result: dict[str, object]
    ) -> None:
        """Submit the completed user/assistant turn without breaking the run."""
        if self._memory_jobs is None or not command.run_id:
            return
        user_text = str(command.payload.get("message", "")).strip()
        if not user_text:
            return
        content = result.get("content")
        if not isinstance(content, str):
            nested = result.get("result")
            content = nested.get("content") if isinstance(nested, dict) else None
        try:
            await self._memory_jobs.enqueue(
                turn_id=command.run_id,
                session_id=command.session_id,
                user_text=user_text,
                assistant_text=content if isinstance(content, str) else "",
            )
        except Exception:
            # Memory extraction is asynchronous and must never turn a
            # successful answer into a failed run.
            return

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
                payload={},
            )
        )


__all__ = ["RunExecutionService"]
