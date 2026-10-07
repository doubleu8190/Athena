"""Provider-neutral LLM and embedding adapters owned by the target package."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    attempts: int = 3
    timeout_seconds: float = 120.0
    delay_seconds: float = 0.0


class TargetLLMProvider:
    """Wrap an injected async model with timeout and bounded retry behavior."""

    def __init__(self, model: Any, *, policy: RetryPolicy | None = None, token_counter: Callable[[str], int] | None = None) -> None:
        self._model = model
        self._policy = policy or RetryPolicy()
        self._token_counter = token_counter or (lambda value: len(value.split()))

    async def invoke(self, messages: list[Any], **kwargs: Any) -> Any:
        last_error: Exception | None = None
        for attempt in range(max(1, self._policy.attempts)):
            try:
                return await asyncio.wait_for(self._model.invoke(messages, **kwargs), timeout=self._policy.timeout_seconds)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last_error = exc
                if attempt + 1 < max(1, self._policy.attempts) and self._policy.delay_seconds:
                    await asyncio.sleep(self._policy.delay_seconds)
        raise RuntimeError("LLM invocation failed") from last_error

    def count_text_tokens(self, text: str) -> int:
        return int(self._token_counter(text))


class TargetEmbeddingProvider:
    """Adapt an injected sync or async embedding callable to a stable API."""

    def __init__(self, embedder: Callable[[Sequence[str]], Any], *, dimension: int) -> None:
        self._embedder = embedder
        self.dimension = dimension

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        result = self._embedder(texts)
        if hasattr(result, "__await__"):
            result = await result
        return [list(map(float, value)) for value in result]

    async def embed_query(self, text: str) -> list[float]:
        values = await self.embed_documents([text])
        if not values:
            raise ValueError("embedding provider returned no vector")
        return values[0]


class _EchoModel:
    """Deterministic local model used when no remote provider is configured."""

    async def invoke(self, messages: list[Any], **_kwargs: Any) -> dict[str, str]:
        value = messages[-1] if messages else ""
        if isinstance(value, dict):
            value = value.get("content", "")
        return {"content": str(value)}


class ConfiguredLLMProvider(TargetLLMProvider):
    """Lazy provider selected from target ``Settings``.

    Imports and network clients are created on first invocation, so building
    the dependency graph remains side-effect free and testable.
    """

    def __init__(self, settings: Any) -> None:
        self._settings = settings
        self._loaded = False
        super().__init__(_EchoModel(), policy=RetryPolicy(
            attempts=settings.llm_retry.max_attempts,
            timeout_seconds=settings.llm_retry.timeout_ms / 1000,
            delay_seconds=settings.llm_retry.min_delay_ms / 1000,
        ))

    def _load_model(self) -> Any:
        config = self._settings.primary_llm
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
            return ChatOllama(model=config.model, base_url=config.base_url or "http://localhost:11434")
        raise ValueError(f"Unsupported LLM provider: {config.provider}")

    async def invoke(self, messages: list[Any], **kwargs: Any) -> Any:
        if not self._loaded:
            self._model = self._load_model()
            self._loaded = True
        return await self._invoke_loaded(messages, **kwargs)

    async def _invoke_loaded(self, messages: list[Any], **kwargs: Any) -> Any:
        # Keep the lazy provider on the same bounded retry contract as the
        # injected provider.  Loading a remote SDK must not silently disable
        # transient-failure recovery.
        return await super().invoke(messages, **kwargs)


class ConfiguredEmbeddingProvider(TargetEmbeddingProvider):
    """Lazy embedding provider selected from target ``Settings``."""

    def __init__(self, settings: Any) -> None:
        self._settings = settings
        self._loaded = False
        super().__init__(self._embed, dimension=settings.embedding_dimension)

    def _load(self) -> Any:
        config = self._settings
        provider = config.embedding_provider.strip().lower()
        if provider in {"echo", "local", "test"}:
            return lambda values: [[float(len(value))] * config.embedding_dimension for value in values]
        if provider in {"sentence_transformers", "sentence-transformers"}:
            from sentence_transformers import SentenceTransformer
            return SentenceTransformer(config.embedding_model, device=config.embedding_device)
        if provider == "openai":
            from langchain_openai import OpenAIEmbeddings
            kwargs: dict[str, Any] = {"model": config.embedding_model, "api_key": config.embedding_api_key}
            if config.embedding_base_url:
                kwargs["base_url"] = config.embedding_base_url
            return OpenAIEmbeddings(**kwargs)
        if provider == "ollama":
            from langchain_ollama import OllamaEmbeddings
            return OllamaEmbeddings(model=config.embedding_model, base_url=config.embedding_base_url or "http://localhost:11434")
        raise ValueError(f"Unsupported embedding provider: {config.embedding_provider}")

    def _embed(self, values: Sequence[str]) -> Any:
        if not self._loaded:
            self._embedder = self._load()
            self._loaded = True
        if callable(self._embedder) and not hasattr(self._embedder, "embed_documents") and not hasattr(self._embedder, "encode"):
            return self._embedder(values)
        if hasattr(self._embedder, "encode"):
            encoded = self._embedder.encode(list(values), convert_to_numpy=True, normalize_embeddings=False)
            return [item.astype(float).tolist() for item in encoded]
        return self._embedder.embed_documents(list(values))


__all__ = [
    "ConfiguredEmbeddingProvider",
    "ConfiguredLLMProvider",
    "RetryPolicy",
    "TargetEmbeddingProvider",
    "TargetLLMProvider",
]
