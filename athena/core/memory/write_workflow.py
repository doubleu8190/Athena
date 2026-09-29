"""Lifecycle orchestration for Memory v2 write processing."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import inspect

from athena.core.memory.distillation import FactExtractor
from athena.core.memory.long_term_memory import LongTermMemoryService
from athena.utils.logging import get_logger

from .contracts import (
    CompletedTurn,
    MemoryCandidate,
    MemoryResolution,
    ResolutionAction,
)
from .candidate_resolver import MemoryCandidateResolver
from .write_trigger import MemoryWriteTrigger

logger = get_logger(__name__)


@dataclass
class FactMemoryWriteOutcome:
    triggered: bool
    candidates: list[MemoryCandidate]
    resolutions: list[MemoryResolution]


class FactMemoryWriteWorkflow:
    """触发、提取、解析并持久化一轮中的原子事实记忆。"""

    def __init__(
        self,
        extractor: FactExtractor,
        memory_service: LongTermMemoryService,
        *,
        trigger: MemoryWriteTrigger | None = None,
        resolver: MemoryCandidateResolver,
    ) -> None:
        self._extractor = extractor
        self._memory = memory_service
        self._trigger = trigger or MemoryWriteTrigger()
        self._resolver = resolver

    async def process_turn(self, turn: CompletedTurn) -> FactMemoryWriteOutcome:
        trigger = self._trigger.evaluate(turn)
        if not trigger.should_extract:
            logger.debug(
                "Memory write trigger did not fire for turn %s: %s",
                turn.turn_id,
                trigger.reason,
            )
            return FactMemoryWriteOutcome(False, [], [])
        existing_memories, related_memories = await self._build_extraction_context(turn)
        candidates = await self._extractor.extract(
            turn, existing_memories=existing_memories
        )
        resolutions = await self._resolver.resolve(
            candidates, related_memories=related_memories
        )
        for index, resolution in enumerate(resolutions):
            candidate = resolution.candidate
            digest = hashlib.sha256(candidate.content.encode("utf-8")).hexdigest()[:16]
            operation_key = (
                f"turn:{turn.turn_id}:resolution:{index}:"
                f"{resolution.action.value}:{resolution.target_memory_id or 'new'}:{digest}"
            )
            if resolution.action is ResolutionAction.CREATE:
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
                    operation_key=operation_key,
                )
                if resolution.relation_type and resolution.target_memory_id:
                    await self._memory.add_memory_relation(
                        new_id,
                        resolution.target_memory_id,
                        resolution.relation_type.value,
                    )
            elif (
                resolution.action
                in {
                    ResolutionAction.UPDATE,
                    ResolutionAction.SUPERSEDE,
                }
                and resolution.target_memory_id
            ):
                # 自动更新从不覆写旧文本。即使解析器给出 UPDATE，也以新版本
                # supersede 旧版本，保留冲突判断、回滚和审计所需的历史。
                revision_kwargs = {
                    "metadata_overrides": {
                        "session_id": turn.session_id,
                        "source_turn_id": turn.turn_id,
                        "type": candidate.memory_type,
                        "category": candidate.category,
                        "confidence": candidate.confidence,
                        "source": "extraction",
                    }
                }
                if "operation_key" in inspect.signature(
                    self._memory.revise_memory
                ).parameters:
                    revision_kwargs["operation_key"] = operation_key
                await self._memory.revise_memory(
                    resolution.target_memory_id,
                    resolution.final_content or candidate.content,
                    **revision_kwargs,
                )
        return FactMemoryWriteOutcome(True, candidates, resolutions)

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
