"""Run execution adapter backed by the Root LangGraph."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass

from langgraph.types import Interrupt

from application.orchestration.protocols import RootGraphPort
from application.runs.ports import RunExecutorPort
from domain.runs import (
    CommandExecutionResult,
    RootWaitCheckpoint,
    RunCommand,
)


@dataclass(slots=True)
class _ActiveRun:
    task: asyncio.Task[CommandExecutionResult]
    stop_signal: asyncio.Event


class LangGraphRunExecutor(RunExecutorPort):
    """Adapt a Root Graph to the application ``RunExecutorPort``.

    Command lifecycle, persistence and events remain application concerns;
    this adapter only translates the graph result into the stable execution
    result contract.
    """

    def __init__(self, graph: RootGraphPort) -> None:
        if graph is None:
            raise ValueError("graph is required")
        self._graph = graph
        self._active: dict[str, _ActiveRun] = {}
        self._lock = asyncio.Lock()

    async def execute(self, command: RunCommand) -> CommandExecutionResult:
        if not command.run_id:
            return CommandExecutionResult(error={"code": "run_id_required"})

        stop_signal = asyncio.Event()
        current = asyncio.current_task()
        if current is None:  # pragma: no cover - asyncio always supplies a task
            raise RuntimeError("run execution requires an asyncio task")
        async with self._lock:
            self._active[command.run_id] = _ActiveRun(current, stop_signal)
        try:
            value = await self._graph.invoke(
                session_id=command.session_id,
                run_id=command.run_id,
                user_message=str(command.payload.get("message", "")),
                message_id=str(command.payload.get("message_id", "")),
                attachment_ids=list(command.payload.get("attachment_ids", ())),
                resume_value=command.payload.get("resume_value"),
                stop_signal=stop_signal,
            )
        finally:
            async with self._lock:
                active = self._active.get(command.run_id)
                if active is not None and active.task is current:
                    self._active.pop(command.run_id, None)
        if isinstance(value, Mapping):
            # 在 LangGraph 边界把框架中断对象转换为领域身份；应用服务不解析框架返回值。
            if "__interrupt__" in value:
                root_wait = _root_wait_checkpoint(value, command.run_id)
                return CommandExecutionResult(
                    root_wait=root_wait,
                    result=dict(value),
                )
            if value.get("error") or value.get("status") == "failed":
                error = value.get("error") or value
                if not isinstance(error, Mapping):
                    return CommandExecutionResult(
                        error={"message": str(error)}
                    )
                return CommandExecutionResult(error=dict(error))
            result = value.get("result", value)
            if not isinstance(result, Mapping):
                return CommandExecutionResult(result={"result": result})
            return CommandExecutionResult(result=dict(result))
        return CommandExecutionResult(result={"result": value})

    async def cancel(self, run_id: str | None) -> None:
        """Stop an active graph invocation for ``run_id``.

        The event gives graph implementations a cooperative cancellation hook;
        cancelling the task also interrupts an in-flight LLM or I/O await.
        Unknown or already-finished runs are intentionally a no-op so command
        retries remain idempotent.
        """
        if not run_id:
            return
        async with self._lock:
            active = self._active.get(run_id)
        if active is None:
            return
        active.stop_signal.set()
        current = asyncio.current_task()
        if active.task is not current and not active.task.done():
            active.task.cancel()


def _root_wait_checkpoint(
    result: Mapping[str, object], expected_run_id: str
) -> RootWaitCheckpoint:
    """Validate LangGraph's sole Round interrupt and convert it to a domain value."""
    interrupts = result["__interrupt__"]
    if not isinstance(interrupts, (list, tuple)) or len(interrupts) != 1:
        raise RuntimeError("root_round_interrupt_count_invalid")
    interrupt = interrupts[0]
    if not isinstance(interrupt, Interrupt):
        raise RuntimeError("root_round_interrupt_invalid")
    value = interrupt.value
    interrupt_id = interrupt.id
    checkpoint_id = result.get("__root_checkpoint_id__")
    if not isinstance(value, Mapping) or value.get("type") != "round_wait":
        raise RuntimeError("root_round_interrupt_payload_invalid")
    run_id = value.get("run_id")
    plan_id = value.get("plan_id")
    round_id = value.get("round_id")
    plan_version = value.get("plan_version")
    if (
        not isinstance(run_id, str)
        or run_id != expected_run_id
        or not isinstance(plan_id, str)
        or not isinstance(round_id, str)
        or not isinstance(plan_version, int)
        or isinstance(plan_version, bool)
        or not isinstance(interrupt_id, str)
        or not isinstance(checkpoint_id, str)
    ):
        raise RuntimeError("root_round_interrupt_identity_invalid")
    return RootWaitCheckpoint(
        run_id=run_id,
        plan_id=plan_id,
        round_id=round_id,
        plan_version=plan_version,
        interrupt_id=interrupt_id,
        checkpoint_id=checkpoint_id,
    )


__all__ = ["LangGraphRunExecutor"]
