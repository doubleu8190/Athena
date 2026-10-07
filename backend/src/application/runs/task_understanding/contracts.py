from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class UserTaskSpec:
    goal: str
    domain: str = "general"
    mode: str = "answer"
    confidence: float = 0.0
    context_requirements: tuple[str, ...] = ("conversation",)
    query_hints: tuple[str, ...] = ()
    attachment_ids: tuple[str, ...] = ()


__all__ = ["UserTaskSpec"]
