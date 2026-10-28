"""Application contracts for extracting and resolving durable memories."""

from __future__ import annotations

from typing import Protocol
from domain.leases import LeaseGrant

from domain.memory import (
    CompletedTurn,
    MemoryCandidate,
    MemoryEpisodeProposal,
    MemoryResolution,
)


class MemoryFactExtractor(Protocol):
    async def extract(
        self, turn: CompletedTurn, *, lease: LeaseGrant
    ) -> list[MemoryCandidate]: ...


class MemoryEpisodeSummarizer(Protocol):
    async def summarize(self, turn: CompletedTurn) -> MemoryEpisodeProposal: ...


class MemoryResolver(Protocol):
    async def resolve(
        self, candidates: list[MemoryCandidate]
    ) -> list[MemoryResolution]: ...


__all__ = [
    "MemoryEpisodeSummarizer",
    "MemoryFactExtractor",
    "MemoryResolver",
]
