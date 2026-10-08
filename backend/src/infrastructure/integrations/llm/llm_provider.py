"""Configured language-model provider."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .options import LLMOptions, coerce_llm_options


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    attempts: int = 3
    timeout_seconds: float = 120.0
    delay_seconds: float = 0.0


class RetryingLLMProvider:
    def __init__(
        self,
        model: Any,
        *,
        policy: RetryPolicy | None = None,
        token_counter: Callable[[str], int] | None = None,
    ) -> None:
        self._model = model
        self._policy = policy or RetryPolicy()
        self._token_counter = token_counter or (lambda value: len(value.split()))

    async def invoke(self, messages: list[Any], **kwargs: Any) -> Any:
        last_error: Exception | None = None
        for attempt in range(max(1, self._policy.attempts)):
            try:
                return await asyncio.wait_for(
                    self._model.invoke(messages, **kwargs),
                    timeout=self._policy.timeout_seconds,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last_error = exc
                if attempt + 1 < max(1, self._policy.attempts) and self._policy.delay_seconds:
                    await asyncio.sleep(self._policy.delay_seconds)
        raise RuntimeError("LLM invocation failed") from last_error

    def count_text_tokens(self, text: str) -> int:
        return int(self._token_counter(text))


class _EchoModel:
    async def invoke(self, messages: list[Any], **_kwargs: Any) -> dict[str, str]:
        value = messages[-1] if messages else ""
        if isinstance(value, dict):
            value = value.get("content", "")
        return {"content": str(value)}


class ConfiguredLLMProvider(RetryingLLMProvider):
    def __init__(self, options: LLMOptions | Any) -> None:
        self._options = coerce_llm_options(options)
        self._loaded = False
        super().__init__(
            _EchoModel(),
            policy=RetryPolicy(
                attempts=self._options.retry.attempts,
                timeout_seconds=self._options.retry.timeout_seconds,
                delay_seconds=self._options.retry.delay_seconds,
            ),
        )

    def _load_model(self) -> Any:
        config = self._options.primary
        provider = config.provider.strip().lower()
        if provider in {"echo", "local", "test"} or not config.api_key:
            return _EchoModel()
        kwargs: dict[str, Any] = {"model": config.model, "api_key": config.api_key}
        if config.base_url:
            kwargs["base_url"] = config.base_url
        if config.temperature >= 0:
            kwargs["temperature"] = config.temperature
        if config.max_tokens >= 0:
            kwargs["max_tokens"] = config.max_tokens
        if provider == "openai":
            from langchain_openai import ChatOpenAI
            return ChatOpenAI(**kwargs)
        if provider == "anthropic":
            from langchain_anthropic import ChatAnthropic
            return ChatAnthropic(**kwargs)
        if provider == "ollama":
            from langchain_ollama import ChatOllama
            kwargs.pop("api_key", None)
            return ChatOllama(
                model=config.model,
                base_url=config.base_url or "http://localhost:11434",
            )
        raise ValueError(f"Unsupported LLM provider: {config.provider}")

    async def invoke(self, messages: list[Any], **kwargs: Any) -> Any:
        if not self._loaded:
            self._model = self._load_model()
            self._loaded = True
        return await super().invoke(messages, **kwargs)


__all__ = ["ConfiguredLLMProvider", "RetryPolicy", "RetryingLLMProvider"]
