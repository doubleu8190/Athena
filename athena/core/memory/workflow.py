"""Lifecycle orchestration for Memory v2 write processing."""

from __future__ import annotations

from dataclasses import dataclass

from athena.core.memory.memory import MemoryManager
from athena.core.memory.summarizer import FactExtractor

from .contracts import (
    CompletedTurn,
    MemoryCandidate,
    MemoryResolution,
    ResolutionAction,
)
from .resolver import MemoryResolver
from .trigger import MemoryTrigger


@dataclass
class MemoryWriteOutcome:
    triggered: bool
    candidates: list[MemoryCandidate]
    resolutions: list[MemoryResolution]


class MemoryWriteWorkflow:
    """Trigger, extract, resolve, and persist one completed turn."""

    def __init__(
        self,
        extractor: FactExtractor,
        memory_manager: MemoryManager,
        *,
        trigger=None,
        resolver=None,
    ) -> None:
        self._extractor = extractor
        self._memory = memory_manager
        self._trigger = trigger or MemoryTrigger()
        self._resolver = resolver or MemoryResolver(memory_manager)

    async def process_turn(self, turn: CompletedTurn) -> MemoryWriteOutcome:
        trigger = self._trigger.evaluate(turn)
        if not trigger.should_extract:
            return MemoryWriteOutcome(False, [], [])
        existing_memories = await self._build_extraction_memories(turn)
        candidates = await self._extractor.extract(
            turn, existing_memories=existing_memories
        )
        resolutions = await self._resolver.resolve(candidates)
        for resolution in resolutions:
            if resolution.action is ResolutionAction.CREATE:
                candidate = resolution.candidate
                await self._memory.add_memory(
                    content=candidate.content,
                    metadata={
                        "session_id": turn.session_id,
                        "source_turn_id": turn.turn_id,
                        "type": candidate.memory_type,
                        "category": candidate.category,
                        "confidence": candidate.confidence,
                        "source": "extraction",
                    },
                )
            elif (
                resolution.action is ResolutionAction.UPDATE
                and resolution.target_memory_id
            ):
                await self._memory.update_memory(
                    resolution.target_memory_id,
                    resolution.final_content or resolution.candidate.content,
                )
            elif (
                resolution.action is ResolutionAction.SUPERSEDE
                and resolution.target_memory_id
            ):
                candidate = resolution.candidate
                new_id = await self._memory.add_memory(
                    content=resolution.final_content or candidate.content,
                    metadata={
                        "session_id": turn.session_id,
                        "source_turn_id": turn.turn_id,
                        "type": candidate.memory_type,
                        "category": candidate.category,
                        "confidence": candidate.confidence,
                        "source": "extraction",
                    },
                )
                await self._memory.supersede(resolution.target_memory_id, new_id)
        return MemoryWriteOutcome(True, candidates, resolutions)

    async def _build_extraction_memories(self, turn: CompletedTurn) -> str:
        """Build the real existing-memory section used by the extraction prompt."""
        query = "\n".join(filter(None, (turn.user_text, turn.assistant_text)))
        try:
            results = await self._memory.search(query, n_results=8, record_access=False)
        except Exception:
            return "(无已有记忆)"
        if not results:
            return "(无已有记忆)"
        lines: list[str] = []
        for item in results:
            content = str(item.get("content", "")).strip()
            if not content:
                continue
            metadata = item.get("metadata") or {}
            category = metadata.get("category") or metadata.get("type")
            suffix = f" (category: {category})" if category else ""
            lines.append(f"- {content}{suffix}")
        return "\n".join(lines) or "(无已有记忆)"
