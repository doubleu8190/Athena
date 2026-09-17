"""Deterministic first-pass resolution of extracted memory candidates."""

from __future__ import annotations

from typing import Any

from langchain_core.messages import HumanMessage

from athena.core.llm.provider import LLMProvider
from athena.core.memory.long_term_memory import LongTermMemoryService
from athena.utils.llm_response import extract_json_from_llm_response, extract_message_text
from athena.utils.prompt_loader import get_prompt

from .contracts import (
    MemoryCandidate,
    MemoryRelationType,
    MemoryResolution,
    ResolutionAction,
)


class MemoryCandidateResolver:
    """Resolve obvious duplicates locally; leave ambiguity to a later LLM step."""

    RESOLUTION_PROMPT = get_prompt("memory_resolution")

    def __init__(
        self,
        memory_service: LongTermMemoryService,
        *,
        similarity_threshold: float = 0.92,
        related_threshold: float = 0.65,
        llm_provider: LLMProvider | None = None,
    ) -> None:
        self._memory = memory_service
        self._threshold = similarity_threshold
        self._related_threshold = related_threshold
        self._llm = llm_provider

    async def resolve(
        self,
        candidates: list[MemoryCandidate],
        *,
        related_memories: list[dict[str, Any]] | None = None,
    ) -> list[MemoryResolution]:
        """
        在智能体长期记忆系统中，新提取的记忆片段可能与已有记忆重复、相似或矛盾。此函数通过相似度阈值和规则/LLM判断来决策最佳处理方式，确保记忆库的干净与一致性。

        Args:
            candidates: list[MemoryCandidate]：待处理的候选记忆列表（每条包含内容、置信度等）。
            related_memories: list[dict[str, Any]] | None = None：可选的相关已有记忆（若提前检索好可避免重复检索）。

        Returns:
            list[MemoryResolution]：每条候选记忆的处理决策，包括操作类型（创建、更新、忽略等）、目标记忆ID（若适用）、最终内容等。
        """

        resolutions: list[MemoryResolution] = []
        seen: set[str] = set()
        for candidate in candidates:
            normalized = " ".join(candidate.content.casefold().split())
            # 处理重复或低置信度的候选记忆
            if not normalized or normalized in seen or candidate.confidence < 0.6:
                resolutions.append(
                    MemoryResolution(
                        action=ResolutionAction.IGNORE,
                        candidate=candidate,
                        reason="duplicate_or_low_confidence",
                    )
                )
                continue
            seen.add(normalized)

            related = (
                related_memories
                if related_memories is not None
                else await self._memory.search(
                    candidate.content, n_results=3, record_access=False
                )
            )

            exact = next(
                (
                    item
                    for item in related
                    if " ".join(str(item.get("content", "")).casefold().split())
                    == normalized
                ),
                None,
            )
            if exact:
                # 在 related 中查找标准化内容完全相同的已有记忆
                resolutions.append(
                    MemoryResolution(
                        action=ResolutionAction.IGNORE,
                        candidate=candidate,
                        target_memory_id=exact.get("id"),
                        reason="exact_existing_memory",
                    )
                )
            elif (
                related
                and max(float(item.get("score", 0.0) or 0.0) for item in related)
                >= self._threshold
            ):
                # 计算 related 中最高相似度得分，若 ≥ self._threshold（通常为较严格的阈值，如 0.9）
                closest = max(
                    related, key=lambda item: float(item.get("score", 0.0) or 0.0)
                )
                resolutions.append(
                    MemoryResolution(
                        action=ResolutionAction.IGNORE,
                        candidate=candidate,
                        target_memory_id=closest.get("id"),
                        reason="high_similarity_existing_memory",
                    )
                )
            elif (
                related
                and max(float(item.get("score", 0.0) or 0.0) for item in related)
                >= self._related_threshold
            ):
                # 若最高得分 ≥ self._related_threshold（较低阈值，如 0.7），说明存在可能相关但并非完全重复的记忆，需要进一步判断
                closest = max(
                    related, key=lambda item: float(item.get("score", 0.0) or 0.0)
                )
                # 非完全重复的高相关事实一律生成新版本。不能从相似度推断
                # 同义改写还是事实冲突，因此保留旧版本比原地覆盖更可靠。
                action = ResolutionAction.SUPERSEDE
                reason = "rule_based_resolution"
                relation_type = (
                    MemoryRelationType.SUPERSEDES
                    if action is ResolutionAction.SUPERSEDE
                    else None
                )
                # 如果配置了 LLM 提供者，则调用 LLM 进行更复杂的语义判断，以决定是否更新、替代或忽略现有记忆
                if self._llm is not None:
                    action, relation_type = await self._llm_resolution(
                        candidate, closest
                    )
                    reason = "llm_resolution"
                resolutions.append(
                    MemoryResolution(
                        action=action,
                        candidate=candidate,
                        target_memory_id=closest.get("id"),
                        final_content=candidate.content,
                        reason=reason,
                        relation_type=relation_type,
                    )
                )
            else:
                # 无相关记忆（相似度低于 _related_threshold）
                resolutions.append(
                    MemoryResolution(
                        action=ResolutionAction.CREATE,
                        candidate=candidate,
                        reason="no_exact_existing_memory",
                    )
                )
        return resolutions

    async def _llm_resolution(
        self, candidate: MemoryCandidate, existing: dict[str, Any]
    ) -> tuple[ResolutionAction, MemoryRelationType | None]:
        """Resolve semantic ambiguity without allowing the model to write data."""
        prompt = self.RESOLUTION_PROMPT.format(
            existing_memory=existing.get("content", ""),
            candidate=candidate.content,
        )
        try:
            response = await self._llm.ainvoke([HumanMessage(content=prompt)])
            data = extract_json_from_llm_response(extract_message_text(response)) or {}
            action = ResolutionAction(str(data.get("action", "UPDATE")).lower())
            relation_value = data.get("relation")
            relation = MemoryRelationType(relation_value) if relation_value else None
            allowed_relation = {
                ResolutionAction.CREATE: {
                    MemoryRelationType.CONTRADICTS,
                    MemoryRelationType.SUPPORTS,
                },
                ResolutionAction.SUPERSEDE: {MemoryRelationType.SUPERSEDES},
            }
            if relation not in allowed_relation.get(action, set()):
                relation = (
                    MemoryRelationType.SUPERSEDES
                    if action is ResolutionAction.SUPERSEDE
                    else None
                )
            return action, relation
        except Exception:
            return ResolutionAction.SUPERSEDE, MemoryRelationType.SUPERSEDES
