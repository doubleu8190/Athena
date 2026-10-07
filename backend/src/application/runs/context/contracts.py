from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class ContextPlan:
    providers: tuple[str, ...] = ("conversation",)
    max_items: int = 20
    max_tokens: int = 4000


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


@dataclass(frozen=True, slots=True)
class ContextBundle:
    items: tuple[ContextItem, ...]
    provider_results: tuple[ProviderResult, ...]
    providers_succeeded: tuple[str, ...] = ()
    providers_failed: tuple[str, ...] = ()
    truncated: bool = False


__all__ = ["ContextBundle", "ContextItem", "ContextPlan", "ProviderResult"]
