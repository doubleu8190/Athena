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
    RunCancelPayload,
    RunCancellationResult,
    RunLifecyclePort,
    RunStartPayload,
    RootWaitCheckpoint,
    RunStatus,
)
from application.runs.ports import RunExecutorPort


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
        # Volatile adapters still need the original user message ID when a
        # resumed run publishes its final answer outside a completion outbox.
        self._user_message_ids_by_run_id: dict[str, str] = {}

    async def execute(self, command: RunCommand) -> dict[str, object] | None:
        """执行取消或 Root 命令，并委托对应分支完成状态收敛。

        Args:
            command: 已从命令队列领取的运行命令。

        Returns:
            已完成命令的结果；等待 Root Resume 时返回 None。

        Raises:
            Exception: 生命周期、事件或命令仓储操作失败时透传。
        """
        if command.command_type == CommandType.RUN_CANCEL:
            return await self._execute_cancellation(command)
        if not command.run_id:
            await self._commands.complete_command(
                command.command_id,
                status=CommandStatus.FAILED,
                error={"code": "run_id_required"},
            )
            return None

        return await self._execute_run(command, command.run_id)

    async def _execute_cancellation(
        self, command: RunCommand
    ) -> dict[str, object]:
        """先持久化取消栅栏，再停止 Root 并完成取消命令。"""
        cancellation: RunCancellationResult | None = None
        if command.run_id:
            if not isinstance(command.payload, RunCancelPayload):
                raise TypeError("run.cancel requires RunCancelPayload")
            cancellation = await self._lifecycle.cancel(
                command.run_id,
                reason=command.payload.reason,
            )

        # 数据库事务先设置取消栅栏，之后才中断 Root，避免新 Worker 越过取消边界。
        cancel_error: Exception | None = None
        try:
            await self._executor.cancel(command.run_id)
        except Exception as error:
            cancel_error = error
        if command.run_id and cancellation and cancellation.changed:
            self._user_message_ids_by_run_id.pop(command.run_id, None)

        if (
            command.run_id
            and cancellation is not None
            and cancellation.changed
            and not self._lifecycle.writes_cancellation_events_to_outbox
        ):
            event_type = (
                EventType.RUN_CANCELLING
                if cancellation.status == RunStatus.CANCELLING
                else EventType.RUN_CANCELLED
            )
            await self._publish(
                command,
                event_type,
                payload={
                    **self._run_payload(command, cancellation.status.value),
                    "external_effect_status": cancellation.external_effect_status,
                },
            )

        if cancel_error is not None:
            raise cancel_error
        result: dict[str, object] = {
            "status": cancellation.status.value if cancellation else "unchanged",
            "external_effect_status": (
                cancellation.external_effect_status if cancellation else "unknown"
            ),
        }
        await self._commands.complete_command(
            command.command_id,
            status=CommandStatus.COMPLETED,
            result=result,
        )
        return result

    async def _execute_run(
        self, command: RunCommand, run_id: str
    ) -> dict[str, object] | None:
        """执行一次 Run/Resume，并按结果分派等待、失败或完成收敛。"""
        started = await self._lifecycle.update_status(
            run_id, RunStatus.RUNNING
        )
        if not started:
            self._user_message_ids_by_run_id.pop(run_id, None)
            result = {"status": RunStatus.CANCELLED.value}
            await self._commands.complete_command(
                command.command_id,
                status=CommandStatus.CANCELLED,
                result=result,
            )
            return result
        if run_id not in self._run_started:
            self._run_started[run_id] = (datetime.now(timezone.utc), perf_counter())
        if isinstance(command.payload, RunStartPayload):
            self._user_message_ids_by_run_id[run_id] = command.payload.message_id

        start_event = (
            EventType.RUN_RESUMED
            if command.command_type == CommandType.RUN_RESUME
            else EventType.RUN_STARTED
        )
        await self._publish(
            command,
            start_event,
            payload=self._run_payload(
                command, "running", started_at=self._run_started[run_id][0]
            ),
        )

        try:
            outcome = await self._executor.execute(command)
        except Exception as exc:
            await self.fail(
                command,
                {"code": "run_failed", "message": str(exc) or type(exc).__name__},
            )
            return None
        # 处理root agent的等待事件
        if outcome.root_wait is not None:
            await self._arm_root_wait(run_id, outcome.root_wait)
            self._waiting_commands[run_id] = command.command_id
            await self._publish(
                command,
                EventType.RUN_ROOT_SUSPENDED,
                payload=self._run_payload(command, "waiting"),
            )
            return None

        if outcome.error is not None:
            await self.fail(command, outcome.error)
            return None

        result = outcome.result or {}
        await self._lifecycle.complete(run_id, result)
        if not self._lifecycle.writes_completion_artifacts_to_outbox:
            # 将结果通过事件发送给用户
            await self._publish_result_message(command, result)
            # 入队一个记忆提取任务
            await self._enqueue_memory(command, result)
            await self._publish(
                command,
                EventType.RUN_COMPLETED,
                payload=self._run_payload(command, "completed", summary="运行已完成"),
            )
        self._run_started.pop(run_id, None)
        self._user_message_ids_by_run_id.pop(run_id, None)
        original_command_id = self._waiting_commands.pop(run_id, None)
        await self._commands.complete_command(
            command.command_id, status=CommandStatus.COMPLETED, result=result
        )
        if original_command_id and original_command_id != command.command_id:
            await self._commands.complete_command(
                original_command_id, status=CommandStatus.COMPLETED, result=result
            )
        await self._commands.complete_active_command_for_run(
            run_id, status=CommandStatus.COMPLETED, result=result
        )
        return result

    async def _arm_root_wait(
        self, run_id: str, root_wait: RootWaitCheckpoint
    ) -> None:
        """登记已由 LangGraph 适配器验证的中断身份。

        Args:
            run_id: 当前运行 ID。
            root_wait: 包含 Run、Plan、Round、interrupt 和 checkpoint 身份的数据。

        Returns:
            成功登记时不返回值。

        Raises:
            ValueError: 等待身份不属于当前 Run。
            RuntimeError: 仓储拒绝登记该 checkpoint。
        """
        if root_wait.run_id != run_id:
            raise ValueError("root_wait_run_identity_mismatch")
        armed = await self._lifecycle.arm_root_wait(
            run_id,
            plan_id=root_wait.plan_id,
            round_id=root_wait.round_id,
            plan_version=root_wait.plan_version,
            interrupt_id=root_wait.interrupt_id,
            checkpoint_id=root_wait.checkpoint_id,
        )
        if not armed:
            raise RuntimeError("root_wait_checkpoint_not_armed")

    async def fail(self, command: RunCommand, error: dict[str, object]) -> None:
        if command.run_id:
            await self._lifecycle.fail(command.run_id, error)
            await self._publish(command, EventType.RUN_FAILED, payload=self._run_payload(command, "failed", error=error))
            self._run_started.pop(command.run_id, None)
            self._user_message_ids_by_run_id.pop(command.run_id, None)
        await self._commands.complete_command(
            command.command_id, status=CommandStatus.FAILED, error=error
        )
        original_command_id = self._waiting_commands.pop(command.run_id, None) if command.run_id else None
        if original_command_id and original_command_id != command.command_id:
            await self._commands.complete_command(
                original_command_id, status=CommandStatus.FAILED, error=error
            )
        if command.run_id:
            await self._commands.complete_active_command_for_run(
                command.run_id, status=CommandStatus.FAILED, error=error
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
        if not isinstance(command.payload, RunStartPayload):
            return
        user_text = command.payload.message.strip()
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
        if not command.run_id:
            return
        if isinstance(command.payload, RunStartPayload):
            message_id = command.payload.message_id
        elif command.command_type == CommandType.RUN_RESUME:
            message_id = self._user_message_ids_by_run_id.get(command.run_id)
        else:
            return
        if not message_id:
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
