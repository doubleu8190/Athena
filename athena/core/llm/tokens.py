"""Provider-aware token counting and response usage extraction."""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessageChunk, BaseMessage


class TokenCounter(Protocol):
    """Token counting capability used by context-budget consumers."""

    def count_text_tokens(self, text: str) -> int: ...

    def count_message_tokens(self, messages: Sequence[BaseMessage]) -> int: ...


@dataclass(frozen=True)
class TokenUsage:
    """Actual token usage reported by an LLM response."""

    input_tokens: int
    output_tokens: int


class ModelTokenCounter:
    """Use a local model tokenizer when reliable, otherwise estimate conservatively."""

    def __init__(self, model: BaseChatModel) -> None:
        self._model = model
        # ChatAnthropic's counter performs a synchronous API request and the generic
        # LangChain/Ollama counter uses a tokenizer unrelated to the selected model.
        self._use_model_tokenizer = type(model).__module__.startswith(
            "langchain_openai."
        )

    def count_text_tokens(self, text: str) -> int:
        if not text:
            return 0
        if self._use_model_tokenizer:
            try:
                return len(self._model.get_token_ids(text))
            except (KeyError, NotImplementedError, ValueError):
                pass
        return conservative_text_token_count(text)

    def count_message_tokens(self, messages: Sequence[BaseMessage]) -> int:
        if not messages:
            return 0
        if self._use_model_tokenizer:
            try:
                return self._model.get_num_tokens_from_messages(list(messages))
            except (KeyError, NotImplementedError, ValueError):
                pass
        return sum(self._count_message_fallback(message) for message in messages) + 3

    def _count_message_fallback(self, message: BaseMessage) -> int:
        payload: dict[str, object] = {
            "role": message.type,
            "content": message.content,
        }
        tool_calls = getattr(message, "tool_calls", None)
        if tool_calls:
            payload["tool_calls"] = tool_calls
        tool_call_id = getattr(message, "tool_call_id", None)
        if tool_call_id:
            payload["tool_call_id"] = tool_call_id
        serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        return conservative_text_token_count(serialized) + 4


def conservative_text_token_count(text: str) -> int:
    """Estimate text tokens without assuming that all scripts tokenize like English."""
    if not text:
        return 0
    ascii_text_count = sum(
        1 for char in text if ord(char) < 128 and (char.isalnum() or char.isspace())
    )
    ascii_punctuation_count = sum(
        1 for char in text if ord(char) < 128 and not (char.isalnum() or char.isspace())
    )
    non_ascii_bytes = sum(len(char.encode("utf-8")) for char in text if ord(char) >= 128)
    return max(
        1,
        math.ceil(
            ascii_text_count / 4
            + ascii_punctuation_count
            + non_ascii_bytes / 2
        ),
    )


def token_usage_from_chunks(chunks: Sequence[AIMessageChunk]) -> TokenUsage | None:
    """Extract normalized usage from a complete stream of LangChain chunks."""
    if not chunks:
        return None
    merged = chunks[0]
    for chunk in chunks[1:]:
        merged = merged + chunk
    usage = _token_usage_from_message(merged)
    if usage is not None:
        return usage
    for chunk in reversed(chunks):
        usage = _token_usage_from_message(chunk)
        if usage is not None:
            return usage
    return None


def _token_usage_from_message(message: BaseMessage) -> TokenUsage | None:
    usage_metadata = getattr(message, "usage_metadata", None)
    if usage_metadata:
        return _build_usage(
            usage_metadata.get("input_tokens"),
            usage_metadata.get("output_tokens"),
        )

    response_metadata = getattr(message, "response_metadata", None) or {}
    for key in ("token_usage", "usage"):
        nested = response_metadata.get(key)
        if isinstance(nested, dict):
            usage = _build_usage(
                nested.get("input_tokens", nested.get("prompt_tokens")),
                nested.get("output_tokens", nested.get("completion_tokens")),
            )
            if usage is not None:
                return usage
    return _build_usage(
        response_metadata.get("prompt_eval_count"),
        response_metadata.get("eval_count"),
    )


def _build_usage(input_tokens: object, output_tokens: object) -> TokenUsage | None:
    if not isinstance(input_tokens, int) or not isinstance(output_tokens, int):
        return None
    if input_tokens < 0 or output_tokens < 0:
        return None
    return TokenUsage(input_tokens=input_tokens, output_tokens=output_tokens)
