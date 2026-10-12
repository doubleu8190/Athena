"""Run execution adapter backed by the Root LangGraph."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass

from domain.orchestration import RootGraphPort
from domain.runs import CommandExecutionResult, RunCommand, RunExecutorPort


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
            if value.get("waiting") or value.get("status") == "waiting":
                return CommandExecutionResult(waiting=True, result=dict(value))
            if value.get("error") or value.get("status") == "failed":
                return CommandExecutionResult(error=dict(value.get("error") or value))
            return CommandExecutionResult(result=dict(value.get("result", value)))
        return CommandExecutionResult(result=value)

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


__all__ = ["LangGraphRunExecutor"]
