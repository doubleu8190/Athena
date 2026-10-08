from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any


@dataclass(frozen=True, slots=True)
class ContextPlan:
    providers: tuple[str, ...] = ("conversation",)
    memory_query: str = ""
    knowledge_query: str = ""
    graph_query: str = ""
    file_query: str = ""
    file_ids: tuple[str, ...] = ()
    knowledge_base_ids: tuple[str, ...] = ()
    max_files: int = 5
    limit_per_file: int = 5
    max_items: int = 20
    max_tokens: int = 4000
    recall_limit: int = 30
    rerank_limit: int = 10

    def with_updates(self, **changes: Any) -> "ContextPlan":
        """Return a plan with selected provider-specific values replaced."""
        return replace(self, **changes)


@dataclass(frozen=True, slots=True)
class ContextItem:
    provider: str
    content: str
    source_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ProviderResult:
    provider: str
    status: str
    items: tuple[ContextItem, ...] = ()
    error_message: str | None = None
    candidate_count: int = 0
    result_count: int = 0
    duration_ms: float = 0.0


@dataclass(frozen=True, slots=True)
class ContextBundle:
    items: tuple[ContextItem, ...]
    provider_results: tuple[ProviderResult, ...]
    providers_succeeded: tuple[str, ...] = ()
    providers_failed: tuple[str, ...] = ()
    truncated: bool = False


__all__ = ["ContextBundle", "ContextItem", "ContextPlan", "ProviderResult"]
