"""记忆生命周期应用层依赖的持久化与来源读取端口。"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from domain.memory.lifecycle import (
    ApplyBatchCommand,
    CleanupResult,
    CompleteJobCommand,
    ControlCommand,
    ControlReceiptResult,
    ControlSourceSnapshot,
    ExtractBatchPayload,
    ExtractBatchResult,
    JobKind,
    MaintainResult,
    MemoryContextSnapshot,
    MemoryJob,
    MemoryPendingRun,
    MemoryReferenceKey,
    MemoryReferenceSnapshot,
    MemoryScopeState,
    MemorySourceMessage,
    ScanResult,
    ScanRunsPayload,
)
from domain.memory.model import AccessContext, Memory, RetrievedMemory


class MemoryLifecycleRepositoryPort(Protocol):
    """连接记忆核心与事务边界的具体 PostgreSQL 能力。"""

    async def list_context(
        self,
        access: AccessContext,
        scope_key: str,
        *,
        limit: int,
    ) -> tuple[Memory, ...]: ...

    async def revalidate_context(
        self,
        access: AccessContext,
        memories: tuple[tuple[str, int, str], ...],
    ) -> MemoryContextSnapshot: ...

    async def revalidate_references(
        self,
        access: AccessContext,
        references: tuple[MemoryReferenceKey, ...],
    ) -> MemoryReferenceSnapshot: ...

    async def retrieve_similar(
        self,
        access: AccessContext,
        scope_key: str,
        query: str,
        embedding: tuple[float, ...],
        embedding_profile_id: str,
        limit: int = 5,
    ) -> tuple[RetrievedMemory, ...]: ...

    async def retrieve_similar_batch(
        self,
        access: AccessContext,
        queries: tuple[tuple[str, str, tuple[float, ...]], ...],
        embedding_profile_id: str,
        limit: int = 5,
    ) -> tuple[tuple[RetrievedMemory, ...], ...]: ...

    async def apply_batch(self, command: ApplyBatchCommand) -> ExtractBatchResult: ...

    async def apply_control(self, command: ControlCommand) -> ControlReceiptResult: ...

    async def control_receipt(
        self,
        *,
        run_id: str,
        tool_call_id: str,
        arguments_digest: str,
    ) -> ControlReceiptResult | None: ...

    async def scope_state(
        self,
        namespace_id: str,
        scope_key: str,
    ) -> MemoryScopeState: ...

    async def wake_user(self, owner_id: str) -> None: ...

    async def user_generation(self, owner_id: str) -> int: ...

    async def ensure_scan_job(self, enabled_boundary_at: datetime) -> MemoryJob: ...

    async def recover_running_jobs(self) -> int: ...

    async def claim_job(
        self,
        *,
        kinds: tuple[JobKind, ...],
        limit: int = 1,
    ) -> MemoryJob | None: ...

    async def complete_job(self, command: CompleteJobCommand) -> bool: ...

    async def list_pending_runs(
        self,
        owner_id: str,
        *,
        limit: int,
        token_budget: int,
    ) -> tuple[MemoryPendingRun, ...]: ...

    async def load_batch_sources(
        self,
        batch: ExtractBatchPayload,
    ) -> tuple[MemorySourceMessage, ...]: ...

    async def skip_runs(
        self,
        owner_id: str,
        run_ids: tuple[str, ...],
        safe_error_code: str,
    ) -> int: ...

    async def list_all_pending_runs(
        self,
        *,
        after_finished_at: datetime | None,
        after_run_id: str | None,
        limit: int,
    ) -> tuple[MemoryPendingRun, ...]: ...

    async def scan_missing_runs(
        self,
        payload: ScanRunsPayload,
        *,
        page_limit: int = 100,
    ) -> ScanResult: ...

    async def enqueue_maintenance(self, day: str) -> int: ...

    async def maintain_scope(
        self,
        namespace_id: str,
        scope_key: str,
        day: str,
        *,
        fact_max_age_days: int,
        other_max_age_days: int,
    ) -> MaintainResult: ...

    async def cleanup_scope(
        self,
        namespace_id: str,
        scope_key: str,
    ) -> CleanupResult: ...


class MemoryControlSourcePort(Protocol):
    """在工具调用时读取固定 Run 的用户消息与实时授权范围。"""

    async def read_control_source(
        self,
        *,
        owner_id: str,
        session_id: str,
        run_id: str,
    ) -> ControlSourceSnapshot | None: ...


__all__ = ["MemoryControlSourcePort", "MemoryLifecycleRepositoryPort"]
