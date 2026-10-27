"""运行查询端口。"""

from __future__ import annotations

from typing import Protocol

from .entities import (
    CommandEnqueueResult,
    CommandStatus,
    CommandStatusRecord,
    RunCancellationResult,
    RunCommand,
    RunStatus,
    RunSummary,
)
from domain.sessions import Session


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

    async def complete_active_command_for_run(
        self,
        run_id: str,
        *,
        status: CommandStatus,
        result: object = None,
        error: object = None,
    ) -> bool: ...


class RunLifecyclePort(Protocol):
    """运行状态转换端口。"""

    writes_cancellation_events_to_outbox: bool
    writes_terminal_artifacts_to_outbox: bool

    async def update_status(self, run_id: str, status: RunStatus | str) -> bool: ...

    async def complete(self, run_id: str, result: dict[str, object]) -> bool: ...

    async def fail(self, run_id: str, error: dict[str, object]) -> bool: ...

    async def cancel(
        self, run_id: str, *, reason: str = "USER_REQUEST"
    ) -> RunCancellationResult | None: ...

    async def arm_root_wait(
        self,
        run_id: str,
        *,
        plan_id: str,
        round_id: str,
        plan_version: int,
        interrupt_id: str,
        checkpoint_id: str,
    ) -> bool: ...


class CommandStorePort(Protocol):
    """命令持久化和幂等入队能力。"""

    async def enqueue_command(
        self, command: RunCommand, *, new_session: Session | None = None
    ) -> CommandEnqueueResult: ...

    async def get_command(self, command_id: str) -> CommandStatusRecord | None: ...


class RunQueryPort(Protocol):
    """读取会话运行摘要的基础设施端口。"""

    async def list_for_session(self, session_id: str) -> list[RunSummary]: ...

    async def list_recent(
        self, *, limit: int, offset: int, session_id: str | None = None,
    ) -> tuple[list[RunSummary], int]: ...

    async def get(self, run_id: str) -> RunSummary | None: ...

    async def get_active_for_session(self, session_id: str) -> RunSummary | None: ...
