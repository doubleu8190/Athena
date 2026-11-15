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
)
from domain.memory.model import AccessContext, Memory, RecallSnapshot, RetrievedMemory


class MemoryRepositoryPort(Protocol):
    """统一的长期记忆召回与生命周期持久化端口。"""

    async def settings(self, namespace_id: str) -> tuple[bool, bool]: ...

    async def search_keyword(
        self, namespace: str, scopes: tuple[str, ...], query: str, limit: int
    ) -> list[tuple[Memory, float]]: ...

    async def search_vector(
        self, namespace: str, scopes: tuple[str, ...], vector: list[float], profile: str, limit: int
    ) -> list[tuple[Memory, float]]: ...

    async def get_many(
        self, ids: tuple[str, ...], namespace: str, scopes: tuple[str, ...]
    ) -> RecallSnapshot: ...

    async def update_settings(
        self, namespace: str, read_enabled: bool, auto_extract_enabled: bool
    ) -> None: ...

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
        *,
        enabled_boundary_at: datetime,
        cursor_finished_at: datetime | None = None,
        cursor_run_id: str | None = None,
        page_limit: int = 100,
    ) -> ScanResult: ...

    async def maintain_due_scopes(
        self,
        day: str,
        *,
        fact_max_age_days: int,
        other_max_age_days: int,
    ) -> int: ...

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


MemoryLifecycleRepositoryPort = MemoryRepositoryPort

__all__ = ["MemoryControlSourcePort", "MemoryLifecycleRepositoryPort", "MemoryRepositoryPort"]
