"""Deterministic first-pass resolution of extracted memory candidates."""

from __future__ import annotations

from typing import Any

from langchain_core.messages import HumanMessage

from athena.core.llm.provider import LLMProvider
from athena.utils.llm import extract_json_from_llm_response, extract_message_text

from .contracts import (
    MemoryCandidate,
    MemoryRelationType,
    MemoryResolution,
    ResolutionAction,
)


class MemoryResolver:
    """Resolve obvious duplicates locally; leave ambiguity to a later LLM step."""

    def __init__(
        self,
        memory_manager: Any,
        *,
        similarity_threshold: float = 0.92,
        related_threshold: float = 0.65,
        llm_provider: LLMProvider | None = None,
    ) -> None:
        self._memory = memory_manager
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
                # 基于规则的初步决策：若候选记忆内容中包含明显的替代、更新或矛盾标记词，则倾向于 SUPERSEDE 或 UPDATE；否则默认 UPDATE
                action = (
                    ResolutionAction.SUPERSEDE
                    if any(
                        marker in candidate.content.casefold()
                        for marker in (
                            "was ",
                            "改成",
                            "换回",
                            "换成",
                            "改为",
                            "替代",
                            "取代",
                            "更新",
                            "现在使用",
                            "不再使用",
                        )
                    )
                    else ResolutionAction.UPDATE
                )
                reason = "rule_based_resolution"
                relation_type = (
                    MemoryRelationType.SUPERSEDES
                    if action is ResolutionAction.SUPERSEDE
                    else None
                )
                # 如果配置了 LLM 提供者，则调用 LLM 进行更复杂的语义判断，以决定是否更新、替代或忽略现有记忆
                if self._llm is not None and action is ResolutionAction.UPDATE:
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
        prompt = (
            "判断新记忆候选与已有记忆的关系，只返回 JSON。\n"
            "action 只能是 UPDATE、SUPERSEDE、IGNORE、CREATE；"
            "relation 只能是 supersedes、contradicts、supports 或 null。"
            "矛盾但无法确认新值替代旧值时用 CREATE+contradicts；"
            "独立证据支持旧记忆时用 CREATE+supports；不确定时返回 UPDATE。\n"
            f"已有记忆：{existing.get('content', '')}\n"
            f"新候选：{candidate.content}\n"
            '{"action":"UPDATE","relation":null}'
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
            return ResolutionAction.UPDATE, None
