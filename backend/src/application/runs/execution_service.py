"""Execute one claimed Run invocation and converge its lifecycle."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from time import perf_counter
from domain.leases import LeaseGrant

from application.runs.ports import RunExecutorPort
from domain.events import ApplicationEvent, EventDurability, EventPort, EventType
from domain.memory import MemoryJobPort
from domain.runs import (
    CommandType,
    RunCommand,
    RunLifecyclePort,
    RunQueryPort,
    RunResumePayload,
    RunStartPayload,
    RunStatus,
    RootWaitCheckpoint,
)


class RunExecutionService:
    """Run Root start/resume calls without a separate command state machine."""

    def __init__(
        self,
        lifecycle: RunLifecyclePort,
        executor: RunExecutorPort,
        events: EventPort,
        memory_jobs: MemoryJobPort | None = None,
        run_query: RunQueryPort | None = None,
    ) -> None:
        self._lifecycle = lifecycle
        self._executor = executor
        self._events = events
        self._memory_jobs = memory_jobs
        self._run_query = run_query
        self._run_started: dict[str, tuple[datetime, float]] = {}
        self._user_message_ids_by_run_id: dict[str, str] = {}

    async def execute(self, invocation: RunCommand) -> dict[str, object] | None:
        if not invocation.run_id:
            return None
        if invocation.command_type not in {CommandType.RUN_START, CommandType.RUN_RESUME}:
            raise TypeError("Run execution accepts only start or resume invocations")
        return await self._execute_run(invocation, invocation.run_id)

    async def interrupt(self, run_id: str) -> None:
        """Signal and cancel a local Root invocation for an accepted stop."""
        await self._executor.cancel(run_id)

    async def _execute_run(
        self, invocation: RunCommand, run_id: str
    ) -> dict[str, object] | None:
        if invocation.lease is None:
            raise RuntimeError("root_execution_lease_missing")
        if not await self._lifecycle.update_status(
            run_id, RunStatus.RUNNING, lease=invocation.lease
        ):
            return None
        if run_id not in self._run_started:
            self._run_started[run_id] = (datetime.now(timezone.utc), perf_counter())
        if isinstance(invocation.payload, RunStartPayload):
            self._user_message_ids_by_run_id[run_id] = invocation.payload.message_id

        event_type = (
            EventType.RUN_RESUMED
            if invocation.command_type == CommandType.RUN_RESUME
            else EventType.RUN_STARTED
        )
        await self._publish(
            invocation,
            event_type,
            payload=self._run_payload(
                invocation, "running", started_at=self._run_started[run_id][0]
            ),
        )

        try:
            outcome = await self._executor.execute(invocation)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self.fail(
                invocation,
                {"code": "run_failed", "message": str(exc) or type(exc).__name__},
            )
            return None

        if outcome.root_wait is not None:
            payload = self._run_payload(invocation, "waiting")
            await self._arm_root_wait(run_id, outcome.root_wait, invocation.lease, payload)
            if self._lifecycle.writes_root_wait_event:
                # 非空序号只用于唤醒订阅者；订阅者从数据库按顺序补读。
                await self._events.publish_realtime(ApplicationEvent(
                    event_type=EventType.RUN_ROOT_SUSPENDED,
                    durability=EventDurability.DURABLE,
                    session_id=invocation.session_id,
                    session_seq=0,
                    run_id=run_id,
                    payload=payload,
                ))
            else:
                await self._publish(invocation, EventType.RUN_ROOT_SUSPENDED, payload=payload)
            return None
        if outcome.error is not None:
            await self.fail(invocation, outcome.error)
            return None

        result = outcome.result or {}
        if invocation.lease is None:
            raise RuntimeError("root_execution_lease_missing")
        if not await self._lifecycle.complete(run_id, result, lease=invocation.lease):
            current = await self._run_query.get(run_id) if self._run_query else None
            status = current.status if current is not None else "unknown"
            if status == RunStatus.STOPPING.value:
                return None
            self._clear_local_state(run_id)
            return {"status": str(getattr(status, "value", status))}

        if not self._lifecycle.writes_terminal_artifacts_to_outbox:
            await self._publish_result_message(invocation, result)
            await self._enqueue_memory(invocation, result)
            await self._publish(
                invocation,
                EventType.RUN_COMPLETED,
                payload=self._run_payload(
                    invocation, "completed", summary="运行已完成"
                ),
            )
        self._clear_local_state(run_id)
        return result

    async def _arm_root_wait(
        self, run_id: str, root_wait: RootWaitCheckpoint, lease: LeaseGrant,
        wait_payload: dict[str, object],
    ) -> None:
        if root_wait.run_id != run_id:
            raise ValueError("root_wait_run_identity_mismatch")
        if not await self._lifecycle.arm_root_wait(
            run_id,
            plan_id=root_wait.plan_id,
            round_id=root_wait.round_id,
            plan_version=root_wait.plan_version,
            interrupt_id=root_wait.interrupt_id,
            checkpoint_id=root_wait.checkpoint_id,
            lease=lease,
            wait_payload=wait_payload,
        ):
            raise RuntimeError("root_wait_checkpoint_not_armed")

    async def fail(self, invocation: RunCommand, error: dict[str, object]) -> None:
        run_id = invocation.run_id
        if run_id is None:
            return
        if invocation.lease is None:
            raise RuntimeError("root_execution_lease_missing")
        changed = await self._lifecycle.fail(
            run_id, error, lease=invocation.lease
        )
        if changed and not self._lifecycle.writes_terminal_artifacts_to_outbox:
            await self._publish(
                invocation,
                EventType.RUN_FAILED,
                payload=self._run_payload(invocation, "failed", error=error),
            )
        self._clear_local_state(run_id)

    def _clear_local_state(self, run_id: str) -> None:
        self._run_started.pop(run_id, None)
        self._user_message_ids_by_run_id.pop(run_id, None)

    async def _publish(
        self,
        invocation: RunCommand,
        event_type: EventType,
        *,
        payload: dict[str, object] | None = None,
    ) -> None:
        await self._events.publish(
            ApplicationEvent(
                event_type=event_type,
                durability=EventDurability.DURABLE,
                session_id=invocation.session_id,
                run_id=invocation.run_id,
                payload=payload or {},
            )
        )

    def _run_payload(
        self,
        invocation: RunCommand,
        status: str,
        *,
        started_at: datetime | None = None,
        summary: str | None = None,
        error: dict[str, object] | None = None,
    ) -> dict[str, object]:
        run_id = invocation.run_id or ""
        payload: dict[str, object] = {
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
            safe_error = {
                key: value for key, value in error.items()
                if key in {"code", "stage", "retryable"}
            }
            safe_error["message"] = str(error.get("code", "run_failed"))
            payload["error"] = safe_error
        return payload

    async def _enqueue_memory(
        self, invocation: RunCommand, result: dict[str, object]
    ) -> None:
        if self._memory_jobs is None or not invocation.run_id:
            return
        if not isinstance(invocation.payload, RunStartPayload):
            return
        user_text = invocation.payload.message.strip()
        if not user_text:
            return
        content = _result_content(result)
        try:
            await self._memory_jobs.enqueue(
                turn_id=invocation.run_id,
                session_id=invocation.session_id,
                user_text=user_text,
                assistant_text=content,
            )
        except Exception:
            return

    async def _publish_result_message(
        self, invocation: RunCommand, result: dict[str, object]
    ) -> None:
        if not invocation.run_id:
            return
        if isinstance(invocation.payload, RunStartPayload):
            reply_to_message_id = invocation.payload.message_id
        elif invocation.command_type == CommandType.RUN_RESUME:
            reply_to_message_id = self._user_message_ids_by_run_id.get(invocation.run_id)
        else:
            return
        if not reply_to_message_id:
            return
        content = _result_content(result)
        if not content:
            return
        answer_stream = result.get("answer_stream")
        if not isinstance(answer_stream, dict):
            nested = result.get("result")
            answer_stream = nested.get("answer_stream") if isinstance(nested, dict) else None
        if not isinstance(answer_stream, dict):
            raise ValueError("answer_stream_metadata_missing")
        stream_id = str(answer_stream["stream_id"])
        assistant_message_id = f"assistant:{invocation.run_id}"
        await self._events.publish(
            ApplicationEvent(
                event_type=EventType.MESSAGE_COMPLETED,
                durability=EventDurability.DURABLE,
                session_id=invocation.session_id,
                run_id=invocation.run_id,
                message_id=assistant_message_id,
                stream_id=stream_id,
                stream_type="answer",
                chunk_id=int(answer_stream["last_chunk_id"]),
                is_complete=True,
                transition_id=f"answer:{stream_id}:completed",
                payload={
                    "content": content,
                    "last_chunk_id": int(answer_stream["last_chunk_id"]),
                    "reply_to_message_id": reply_to_message_id,
                    "kind": result.get("kind", "final"),
                    "started_at": answer_stream["started_at"],
                    "status": "completed",
                },
            )
        )


def _result_content(result: dict[str, object]) -> str:
    value = result.get("content")
    if isinstance(value, str):
        return value
    nested = result.get("result")
    if isinstance(nested, dict) and isinstance(nested.get("content"), str):
        return str(nested["content"])
    return ""


__all__ = ["RunExecutionService"]
