"""长期记忆记录的生命周期端口。"""

from __future__ import annotations

from typing import Protocol

from domain.leases import LeaseGrant
from .contracts import MemoryFact, MemoryFactIdentity
from .entities import MemoryListRequest, MemoryPage, MemoryRecord, MemoryRelationRecord, MemorySearchRequest, MemoryWriteCommand


class MemoryRepositoryPort(Protocol):
    async def initialize(self) -> None: ...
    async def search(self, request: MemorySearchRequest) -> list[MemoryRecord]: ...
    async def list_scope_index(
        self,
        *,
        session_id: str,
        project_id: str | None,
        limit: int = 80,
    ) -> list[MemoryRecord]: ...
    async def list_memories(self, request: MemoryListRequest) -> MemoryPage: ...
    async def list_fact_group(
        self, fact: MemoryFact, *, session_id: str
    ) -> list[MemoryRecord]: ...
    async def list_fact_group_by_identity(
        self, identity: MemoryFactIdentity
    ) -> list[MemoryRecord]: ...
    async def get(self, memory_id: str) -> MemoryRecord | None: ...
    async def revisions(self, memory_id: str) -> list[MemoryRecord]: ...
    async def related_entries(self, memory_id: str) -> list[MemoryRelationRecord]: ...
    async def create_memory(self, command: MemoryWriteCommand) -> str: ...
    async def create_memory_from_worker(
        self, command: MemoryWriteCommand, *, lease: LeaseGrant
    ) -> str: ...
    async def revise(
        self,
        memory_id: str,
        content: str,
        *,
        metadata: dict[str, object] | None = None,
        operation_key: str | None = None,
        expected_memory_ids: tuple[str, ...] | None = None,
    ) -> str | None: ...
    async def revise_from_worker(
        self,
        memory_id: str,
        content: str,
        *,
        metadata: dict[str, object] | None = None,
        operation_key: str | None = None,
        expected_memory_ids: tuple[str, ...] | None = None,
        lease: LeaseGrant,
    ) -> str | None: ...
    async def add_relation(
        self,
        source_memory_id: str,
        target_memory_id: str,
        kind: str,
        *,
        operation_id: str,
    ) -> None: ...
    async def add_relation_from_worker(
        self,
        source_memory_id: str,
        target_memory_id: str,
        kind: str,
        *,
        operation_id: str,
        lease: LeaseGrant,
    ) -> None: ...
    async def update_validity(self, memory_id: str, validity_status: str, *, valid_until: str | None = None) -> bool: ...
    async def delete(self, memory_id: str) -> bool: ...
    async def record_access(self, memory_ids: list[str]) -> int: ...
    async def flush_access_stats(self) -> int: ...


__all__ = ["MemoryRepositoryPort"]
