"""Options consumed by provider-neutral LLM adapters."""

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class LLMProviderOptions:
    name: str = "primary"
    provider: str = "openai"
    model: str = "gpt-4o"
    api_key: str = ""
    base_url: str = ""
    temperature: float = -1
    max_tokens: int = -1


@dataclass(frozen=True, slots=True)
class RetryOptions:
    attempts: int = 3
    timeout_seconds: float = 60.0
    delay_seconds: float = 2.0


@dataclass(frozen=True, slots=True)
class LLMOptions:
    providers: tuple[LLMProviderOptions, ...] = field(
        default_factory=lambda: (LLMProviderOptions(),)
    )
    retry: RetryOptions = field(default_factory=RetryOptions)

    @property
    def primary(self) -> LLMProviderOptions:
        if not self.providers:
            raise ValueError("providers must not be empty")
        return self.providers[0]


@dataclass(frozen=True, slots=True)
class EmbeddingOptions:
    provider: str = "sentence_transformers"
    model: str = "BAAI/bge-m3"
    device: str = "cpu"
    dimension: int = 1024
    api_key: str = ""
    base_url: str = ""


def coerce_llm_options(value: LLMOptions | Any) -> LLMOptions:
    """将运行时 Settings 或已类型化选项转换为 LLMOptions。"""
    if isinstance(value, LLMOptions):
        return value
    retry = value.llm_retry
    return LLMOptions(
        providers=tuple(
            LLMProviderOptions(
                name=item.name,
                provider=item.provider,
                model=item.model,
                api_key=item.api_key,
                base_url=item.base_url,
                temperature=item.temperature,
                max_tokens=item.max_tokens,
            )
            for item in value.llm_providers
        ),
        retry=RetryOptions(
            attempts=retry.max_attempts,
            timeout_seconds=retry.timeout_ms / 1000,
            delay_seconds=retry.min_delay_ms / 1000,
        ),
    )


def coerce_embedding_options(value: EmbeddingOptions | Any) -> EmbeddingOptions:
    """将运行时 Settings 或已类型化选项转换为 EmbeddingOptions。"""
    if isinstance(value, EmbeddingOptions):
        return value
    return EmbeddingOptions(
        provider=value.embedding_provider,
        model=value.embedding_model,
        device=value.embedding_device,
        dimension=value.embedding_dimension,
        api_key=value.embedding_api_key,
        base_url=value.embedding_base_url,
    )


__all__ = [
    "EmbeddingOptions",
    "LLMOptions",
    "LLMProviderOptions",
    "RetryOptions",
    "coerce_llm_options",
    "coerce_embedding_options",
]
