from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .contracts import UserTaskSpec


@dataclass(frozen=True, slots=True)
class TaskUnderstandingResult:
    task: UserTaskSpec
    source: str


class TaskUnderstandingService:
    """Use deterministic fast paths before an optional structured LLM."""

    def __init__(self, structured_llm=None, *, timeout_seconds: float = 15.0) -> None:
        self._structured_llm = structured_llm
        self._timeout_seconds = timeout_seconds

    async def understand(self, *, session_id: str, user_message: str, history: list[dict[str, Any]] | None = None, attachment_refs: list[dict[str, Any]] | None = None, knowledge_bases: list[dict[str, Any]] | None = None) -> TaskUnderstandingResult:
        text = user_message.strip()
        attachments = tuple(str(item.get("id", "")) for item in (attachment_refs or []) if item.get("id"))
        if not text:
            return TaskUnderstandingResult(UserTaskSpec("answer the user", attachment_ids=attachments), "fallback")
        if self._structured_llm is None:
            return TaskUnderstandingResult(self._fast_path(text, attachments), "fast_path")
        try:
            value = await self._structured_llm.generate(text, history=history or [], knowledge_bases=knowledge_bases or [])
            if isinstance(value, UserTaskSpec):
                return TaskUnderstandingResult(value, "llm")
        except Exception:
            pass
        return TaskUnderstandingResult(self._fast_path(text, attachments), "fallback")

    @staticmethod
    def _fast_path(text: str, attachments: tuple[str, ...]) -> UserTaskSpec:
        lowered = text.lower()
        mode = "search" if any(value in lowered for value in ("search", "find", "查找", "检索")) else "answer"
        requirements = ("file", "conversation") if attachments else ("conversation",)
        return UserTaskSpec(text[:1000], mode=mode, confidence=1.0, context_requirements=requirements, attachment_ids=attachments)


__all__ = ["TaskUnderstandingResult", "TaskUnderstandingService"]
