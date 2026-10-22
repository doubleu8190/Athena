"""Application contracts for extracting and resolving durable memories."""

from __future__ import annotations

from typing import Protocol

from domain.memory import CompletedTurn, MemoryCandidate, MemoryResolution


class MemoryWriteTrigger(Protocol):
    def should_process(self, turn: CompletedTurn) -> bool: ...


class MemoryFactExtractor(Protocol):
    async def extract(self, turn: CompletedTurn) -> list[MemoryCandidate]: ...


class MemoryResolver(Protocol):
    async def resolve(
        self, candidates: list[MemoryCandidate]
    ) -> list[MemoryResolution]: ...


__all__ = ["MemoryFactExtractor", "MemoryResolver", "MemoryWriteTrigger"]
