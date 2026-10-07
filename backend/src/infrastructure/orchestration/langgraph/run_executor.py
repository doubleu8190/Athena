"""Run execution adapter backed by the Root LangGraph."""

from __future__ import annotations

from collections.abc import Mapping
from domain.orchestration import RootGraphPort
from domain.runs import CommandExecutionResult, RunCommand, RunExecutorPort


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

    async def execute(self, command: RunCommand) -> CommandExecutionResult:
        if not command.run_id:
            return CommandExecutionResult(error={"code": "run_id_required"})

        value = await self._graph.invoke(
            session_id=command.session_id,
            run_id=command.run_id,
            user_message=str(command.payload.get("message", "")),
            message_id=str(command.payload.get("message_id", "")),
            attachment_ids=list(command.payload.get("attachment_ids", ())),
            resume_value=command.payload.get("resume_value"),
        )
        if isinstance(value, Mapping):
            if value.get("waiting") or value.get("status") == "waiting":
                return CommandExecutionResult(waiting=True, result=dict(value))
            if value.get("error") or value.get("status") == "failed":
                return CommandExecutionResult(error=dict(value.get("error") or value))
            return CommandExecutionResult(result=dict(value.get("result", value)))
        return CommandExecutionResult(result=value)

    async def cancel(self, run_id: str | None) -> None:
        """Cancellation is delivered through the runtime stop signal."""
        return None


__all__ = ["LangGraphRunExecutor"]
