"""长期记忆写入分析端口。"""

from __future__ import annotations

from typing import Protocol

from .entities import CompletedTurn, MemoryCandidate, MemoryResolution


class MemoryWriteTriggerPort(Protocol):
    def should_process(self, turn: CompletedTurn) -> bool: ...


class MemoryFactExtractorPort(Protocol):
    async def extract(self, turn: CompletedTurn) -> list[MemoryCandidate]: ...


class MemoryResolverPort(Protocol):
    async def resolve(self, candidates: list[MemoryCandidate]) -> list[MemoryResolution]: ...


__all__ = ["MemoryFactExtractorPort", "MemoryResolverPort", "MemoryWriteTriggerPort"]
