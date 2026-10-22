"""Application execution contracts for run command handling."""

from __future__ import annotations

from typing import Protocol

from domain.runs import CommandExecutionResult, RunCommand


class RunExecutorPort(Protocol):
    """Execute or cancel a run command through the configured runtime."""

    async def execute(self, command: RunCommand) -> CommandExecutionResult: ...

    async def cancel(self, run_id: str | None) -> None: ...


__all__ = ["RunExecutorPort"]
