"""运行查询端口。"""

from __future__ import annotations

from typing import Protocol

from .entities import (
    CommandEnqueueResult,
    CommandExecutionResult,
    CommandStatus,
    CommandStatusRecord,
    RunCommand,
    RunStatus,
    RunSummary,
)


class CommandQueuePort(Protocol):
    """后台 Worker 使用的命令领取和完成端口。"""

    async def claim_next_command(self) -> RunCommand | None: ...

    async def complete_command(
        self,
        command_id: str,
        *,
        status: CommandStatus,
        result: object = None,
        error: object = None,
    ) -> None: ...


class RunLifecyclePort(Protocol):
    """运行状态转换端口。"""

    async def update_status(self, run_id: str, status: RunStatus | str) -> bool: ...

    async def complete(self, run_id: str, result: dict[str, object]) -> bool: ...

    async def fail(self, run_id: str, error: dict[str, object]) -> bool: ...

    async def cancel(self, run_id: str) -> bool: ...


class RunExecutorPort(Protocol):
    """一次运行的实际执行能力，由 Runtime/LangGraph 适配器提供。"""

    async def execute(self, command: RunCommand) -> CommandExecutionResult: ...

    async def cancel(self, run_id: str | None) -> None: ...


class CommandStorePort(Protocol):
    """命令持久化和幂等入队能力。"""

    async def enqueue_command(self, command: RunCommand) -> CommandEnqueueResult: ...

    async def get_command(self, command_id: str) -> CommandStatusRecord | None: ...


class RunQueryPort(Protocol):
    """读取会话运行摘要的基础设施端口。"""

    async def list_for_session(self, session_id: str) -> list[RunSummary]: ...

    async def get(self, run_id: str) -> RunSummary | None: ...

    async def get_active_for_session(self, session_id: str) -> RunSummary | None: ...
