"""Run queue, lifecycle, submission and query ports."""

from __future__ import annotations

from typing import Protocol

from domain.sessions import Session

from .entities import (
    RunCancellationResult,
    RunCommand,
    RunEnqueueResult,
    RunStatus,
    RunSummary,
)
from .payloads import RunStartPayload


class RunQueuePort(Protocol):
    """Claim the FIFO head of an unlocked session queue."""

    async def claim_next_run(self) -> RunCommand | None: ...


class RunConsumerPort(Protocol):
    """Persistence methods needed by the asynchronous Run consumer."""

    async def claim_next_run(self) -> RunCommand | None:
        """领取最早的可执行 Run；没有空闲会话时返回 None。"""

    async def reconcile_expired_execution_leases(self) -> int:
        """将失去 Root 调用租约的运行推进到失败收尾。"""

    async def get(self, run_id: str) -> RunSummary | None:
        """按 ID 查询 Run；不存在时返回 None。"""

    async def release_execution_lease(
        self, run_id: str, *, owner: str, generation: int
    ) -> bool:
        """释放指定执行代次持有的租约；租约已变化时返回 False。"""

    async def renew_execution_lease(
        self,
        run_id: str,
        *,
        owner: str,
        generation: int,
        lease_seconds: float,
    ) -> bool:
        """续期当前 Root 调用的租约；执行权已变化时返回 False。"""

    async def finalize_stop(self, run_id: str, *, failed: bool = False) -> bool:
        """确认执行端已停止后结束 Run；尚有租约或 Worker 时返回 False。"""


class RunSubmissionPort(Protocol):
    """Persist a source message, queued Run and idempotency receipt atomically."""

    async def enqueue_run(
        self,
        *,
        request_id: str,
        parameters: dict[str, object],
        session_id: str,
        project_id: str | None,
        run_id: str,
        payload: RunStartPayload,
        new_session: Session | None = None,
    ) -> tuple[RunEnqueueResult, bool]: ...


class RunLifecyclePort(Protocol):
    """Run state transitions and atomic stop acceptance."""

    writes_cancellation_events_to_outbox: bool
    writes_terminal_artifacts_to_outbox: bool

    async def update_status(self, run_id: str, status: RunStatus | str) -> bool: ...

    async def complete(
        self, run_id: str, result: dict[str, object], *,
        owner: str | None = None, generation: int | None = None,
    ) -> bool: ...

    async def fail(
        self, run_id: str, error: dict[str, object], *,
        owner: str | None = None, generation: int | None = None,
    ) -> bool: ...

    async def acquire_execution_lease(
        self, run_id: str, *, owner: str, lease_seconds: float
    ) -> int | None: ...

    async def renew_execution_lease(
        self,
        run_id: str,
        *,
        owner: str,
        generation: int,
        lease_seconds: float,
    ) -> bool: ...

    async def release_execution_lease(
        self, run_id: str, *, owner: str, generation: int
    ) -> bool: ...

    async def stop_active(
        self,
        session_id: str,
        *,
        request_id: str,
        expected_run_id: str | None,
        displayed_session_seq: int,
    ) -> tuple[RunCancellationResult, bool]: ...

    async def finalize_stop(self, run_id: str, *, failed: bool = False) -> bool: ...

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


class RunQueryPort(Protocol):
    """Read persisted Run summaries and session queue state."""

    async def list_for_session(self, session_id: str) -> list[RunSummary]: ...

    async def list_recent(
        self, *, limit: int, offset: int, session_id: str | None = None,
    ) -> tuple[list[RunSummary], int]: ...

    async def get(self, run_id: str) -> RunSummary | None: ...

    async def get_active_for_session(self, session_id: str) -> RunSummary | None: ...


__all__ = [
    "RunLifecyclePort",
    "RunQueuePort",
    "RunQueryPort",
    "RunSubmissionPort",
]
