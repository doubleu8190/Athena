"""Lifecycle orchestration for Memory v2 write processing."""

from __future__ import annotations

from dataclasses import dataclass

from athena.core.memory.distillation import FactExtractor
from athena.core.memory.long_term_memory import LongTermMemoryService

from .contracts import (
    CompletedTurn,
    MemoryCandidate,
    MemoryResolution,
    ResolutionAction,
)
from .candidate_resolver import MemoryCandidateResolver
from .write_trigger import MemoryWriteTrigger


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
        memory_service: LongTermMemoryService,
        *,
        trigger: MemoryWriteTrigger | None = None,
        resolver: MemoryCandidateResolver | None = None,
    ) -> None:
        self._extractor = extractor
        self._memory = memory_service
        self._trigger = trigger or MemoryWriteTrigger()
        self._resolver = resolver or MemoryCandidateResolver(memory_service)

    async def process_turn(self, turn: CompletedTurn) -> MemoryWriteOutcome:
        trigger = self._trigger.evaluate(turn)
        if not trigger.should_extract:
            return MemoryWriteOutcome(False, [], [])
        existing_memories, related_memories = await self._build_extraction_context(turn)
        candidates = await self._extractor.extract(
            turn, existing_memories=existing_memories
        )
        resolutions = await self._resolver.resolve(
            candidates, related_memories=related_memories
        )
        for resolution in resolutions:
            if resolution.action is ResolutionAction.CREATE:
                candidate = resolution.candidate
                new_id = await self._memory.add_memory(
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
                if resolution.relation_type and resolution.target_memory_id:
                    await self._memory.add_memory_relation(
                        new_id,
                        resolution.target_memory_id,
                        resolution.relation_type.value,
                    )
            elif resolution.action in {
                ResolutionAction.UPDATE,
                ResolutionAction.SUPERSEDE,
            } and resolution.target_memory_id:
                candidate = resolution.candidate
                # 自动更新从不覆写旧文本。即使解析器给出 UPDATE，也以新版本
                # supersede 旧版本，保留冲突判断、回滚和审计所需的历史。
                await self._memory.revise_memory(
                    resolution.target_memory_id,
                    resolution.final_content or candidate.content,
                    metadata_overrides={
                        "session_id": turn.session_id,
                        "source_turn_id": turn.turn_id,
                        "type": candidate.memory_type,
                        "category": candidate.category,
                        "confidence": candidate.confidence,
                        "source": "extraction",
                    },
                )
        return MemoryWriteOutcome(True, candidates, resolutions)

    async def _build_extraction_context(
        self, turn: CompletedTurn
    ) -> tuple[str, list[dict]]:
        """Retrieve once and share related memories with extraction and resolution."""
        query = "\n".join(filter(None, (turn.user_text, turn.assistant_text)))
        try:
            results = await self._memory.search(query, n_results=8, record_access=False)
        except Exception:
            return "(无已有记忆)", []
        if not results:
            return "(无已有记忆)", []
        lines: list[str] = []
        for item in results:
            content = str(item.get("content", "")).strip()
            if not content:
                continue
            metadata = item.get("metadata") or {}
            category = metadata.get("category") or metadata.get("type")
            suffix = f" (category: {category})" if category else ""
            lines.append(f"- {content}{suffix}")
        return "\n".join(lines) or "(无已有记忆)", results
