"""Deterministic first-pass resolution of extracted memory candidates."""

from __future__ import annotations

from typing import Any

from langchain_core.messages import HumanMessage

from athena.utils.llm import extract_json_from_llm_response, extract_message_text

from .contracts import MemoryCandidate, MemoryResolution, ResolutionAction


class MemoryResolver:
    """Resolve obvious duplicates locally; leave ambiguity to a later LLM step."""

    def __init__(
        self,
        memory_manager: Any,
        *,
        similarity_threshold: float = 0.92,
        related_threshold: float = 0.65,
        llm_provider: Any | None = None,
    ) -> None:
        self._memory = memory_manager
        self._threshold = similarity_threshold
        self._related_threshold = related_threshold
        self._llm = llm_provider

    async def resolve(
        self, candidates: list[MemoryCandidate]
    ) -> list[MemoryResolution]:
        resolutions: list[MemoryResolution] = []
        seen: set[str] = set()
        for candidate in candidates:
            normalized = " ".join(candidate.content.casefold().split())
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
            related = await self._memory.search(
                candidate.content, n_results=3, record_access=False
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
            elif related and max(
                float(item.get("score", 0.0) or 0.0) for item in related
            ) >= self._related_threshold:
                closest = max(
                    related, key=lambda item: float(item.get("score", 0.0) or 0.0)
                )
                action = (
                    ResolutionAction.SUPERSEDE
                    if any(
                        marker in candidate.content.casefold()
                        for marker in (
                            "was ", "改成", "换回", "换成", "改为", "替代", "取代",
                            "更新", "现在使用", "不再使用",
                        )
                    )
                    else ResolutionAction.UPDATE
                )
                reason = "rule_based_resolution"
                if self._llm is not None and action is ResolutionAction.UPDATE:
                    action = await self._llm_resolution(candidate, closest)
                    reason = "llm_resolution"
                resolutions.append(
                    MemoryResolution(
                        action=action,
                        candidate=candidate,
                        target_memory_id=closest.get("id"),
                        final_content=candidate.content,
                        reason=reason,
                    )
                )
            else:
                resolutions.append(
                    MemoryResolution(
                        action=ResolutionAction.CREATE,
                        candidate=candidate,
                        reason="no_exact_existing_memory",
                    )
                )
        return resolutions

    async def _llm_resolution(self, candidate: MemoryCandidate, existing: dict[str, Any]) -> ResolutionAction:
        """Resolve semantic ambiguity without allowing the model to write data."""
        prompt = (
            "判断新记忆候选与已有记忆的关系，只返回 JSON。\n"
            "action 只能是 UPDATE、SUPERSEDE、IGNORE；不确定时返回 UPDATE。\n"
            f"已有记忆：{existing.get('content', '')}\n"
            f"新候选：{candidate.content}\n"
            '{"action":"UPDATE"}'
        )
        try:
            response = await self._llm.ainvoke([HumanMessage(content=prompt)])
            data = extract_json_from_llm_response(extract_message_text(response)) or {}
            return ResolutionAction(str(data.get("action", "UPDATE")).lower())
        except Exception:
            return ResolutionAction.UPDATE
