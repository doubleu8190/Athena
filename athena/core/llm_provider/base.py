"""LLM Provider abstract base class and response model."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class LLMResponse:
    """Standardized LLM response across all providers.

    tool_calls format: [{"name": "tool_name", "arguments": {...}}]
    """
    text: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    token_usage: dict[str, int] = field(default_factory=lambda: {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    })
    model: str = ""
    finish_reason: str = "stop"


class LLMProvider(ABC):
    """Abstract base for LLM providers.

    All Athena providers MUST support native Function Calling (tool use).
    Providers that require parsing-based tool call emulation are NOT accepted.
    """

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Unique provider identifier (e.g. 'openai', 'anthropic')."""
        ...

    @abstractmethod
    async def generate(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> LLMResponse:
        """Generate a completion with optional tool calls.

        Args:
            messages: Chat messages in standard format.
            tools: Tool definitions for native Function Calling.
            max_tokens: Maximum completion tokens.
            temperature: Sampling temperature.

        Returns:
            LLMResponse with text, tool_calls, and token_usage.
        """
        ...

    @abstractmethod
    async def generate_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ):
        """Generate a streaming completion. Yields LLMResponse chunks."""
        ...

    def _estimate_tokens(self, text: str) -> int:
        """Rough token estimation fallback when tiktoken is not available."""
        # ~4 characters per token for English
        return max(1, len(text) // 4)
