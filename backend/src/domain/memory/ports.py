"""长期记忆用例需要的外部能力端口。"""

from __future__ import annotations

from typing import Protocol

from .entities import (
    MemoryListRequest,
    MemoryPage,
    MemoryRecord,
    MemorySearchRequest,
    MemoryWriteCommand,
    CompletedTurn,
    MemoryCandidate,
    MemoryResolution,
)


class MemoryPort(Protocol):
    """长期记忆生命周期、检索和版本操作能力。"""

    async def initialize(self) -> None: ...

    async def search(self, request: MemorySearchRequest) -> list[MemoryRecord]: ...

    async def list(self, request: MemoryListRequest) -> MemoryPage: ...

    async def get(self, memory_id: str) -> MemoryRecord | None: ...

    async def revisions(self, memory_id: str) -> list[MemoryRecord]: ...

    async def add(self, command: MemoryWriteCommand) -> str: ...

    async def revise(
        self,
        memory_id: str,
        content: str,
        *,
        metadata: dict[str, object] | None = None,
        operation_key: str | None = None,
    ) -> str | None: ...

    async def set_validity(
        self,
        memory_id: str,
        validity_status: str,
        *,
        valid_until: str | None = None,
    ) -> bool: ...

    async def delete(self, memory_id: str) -> None: ...

    async def flush_access_stats(self) -> int: ...


class MemoryWriteTriggerPort(Protocol):
    """判断一轮对话是否需要进入事实提取。"""

    def should_process(self, turn: CompletedTurn) -> bool: ...


class MemoryFactExtractorPort(Protocol):
    """从对话中提取候选事实。"""

    async def extract(self, turn: CompletedTurn) -> list[MemoryCandidate]: ...


class MemoryResolverPort(Protocol):
    """将候选事实解析为持久化动作。"""

    async def resolve(self, candidates: list[MemoryCandidate]) -> list[MemoryResolution]: ...


class MemoryJobPort(Protocol):
    """记忆后台任务队列。"""

    async def recover(self) -> None: ...
    async def claim(self) -> dict[str, object] | None: ...
    async def succeed(self, turn_id: str) -> None: ...
    async def fail(self, turn_id: str, error: str, *, retry: bool) -> None: ...


__all__ = [
    "MemoryFactExtractorPort",
    "MemoryJobPort",
    "MemoryPort",
    "MemoryResolverPort",
    "MemoryWriteTriggerPort",
]
