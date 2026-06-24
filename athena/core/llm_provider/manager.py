"""LLM Provider Manager.

Manages provider instances, fallback chain dispatch, and provider resolution.
"""

from __future__ import annotations

from typing import Any

from athena.config import Config, LLMConfig
from athena.core.llm_provider.base import LLMProvider, LLMResponse
from athena.core.llm_provider.openai import OpenAIProvider
from athena.core.llm_provider.anthropic import AnthropicProvider
from athena.core.llm_provider.deepseek import DeepSeekProvider
from athena.core.llm_provider.litellm import LiteLLMProvider
from athena.logging_config import get_logger

logger = get_logger(__name__)


class LLMProviderManager:
    """Manages LLM provider lifecycle and fallback dispatch.

    On generate(), tries default_provider first. On failure, iterates
    through fallback_chain in order. If all fail, raises the last error.
    """

    def __init__(self, config: Config):
        self._config = config
        self._providers: dict[str, LLMProvider] = {}
        self._default_provider = config.llm.default_provider
        self._fallback_chain = config.llm.fallback_chain
        self._init_providers()

    def _init_providers(self) -> None:
        """Instantiate all configured providers."""
        for name, pdata in self._config.llm.providers.items():
            provider = self._create_provider(name, pdata)
            if provider:
                self._providers[name] = provider
                logger.info("llm_provider_registered", name=name, model=provider.model)

    def _create_provider(self, name: str, pdata: Any) -> LLMProvider | None:
        """Create a provider instance based on configuration."""
        # Determine provider class by name convention
        if name.lower().startswith("anthropic"):
            return AnthropicProvider(
                api_key_env=pdata.api_key_env,
                model=pdata.model,
                base_url=pdata.base_url,
            )
        elif name.lower().startswith("deepseek"):
            return DeepSeekProvider(
                api_key_env=pdata.api_key_env,
                model=pdata.model,
                base_url=pdata.base_url,
            )
        elif name.lower().startswith("litellm"):
            return LiteLLMProvider(
                api_key_env=pdata.api_key_env,
                model=pdata.model,
                base_url=pdata.base_url or "http://litellm:4000",
            )
        else:
            # Default to OpenAI-compatible (covers OpenAI, vLLM, etc.)
            return OpenAIProvider(
                api_key_env=pdata.api_key_env,
                model=pdata.model,
                base_url=pdata.base_url,
            )

    def get_provider(self, name: str) -> LLMProvider | None:
        """Get a registered provider by name."""
        return self._providers.get(name)

    @property
    def default_provider_name(self) -> str:
        return self._default_provider

    async def generate(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        provider_name: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> LLMResponse:
        """Generate a completion with fallback chain support.

        Tries providers in order: explicit provider_name → default_provider
        → fallback_chain. Stops at the first successful call.
        """
        ordered_providers = []

        if provider_name and provider_name in self._providers:
            ordered_providers.append(provider_name)
        else:
            ordered_providers.append(self._default_provider)
            ordered_providers.extend(
                p for p in self._fallback_chain if p != self._default_provider
            )

        last_error = None
        for name in ordered_providers:
            provider = self._providers.get(name)
            if not provider:
                logger.warning("llm_provider_not_found", name=name)
                continue

            try:
                response = await provider.generate(
                    messages=messages,
                    tools=tools,
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
                logger.info(
                    "llm_generate_success",
                    provider=name,
                    model=response.model,
                    total_tokens=response.token_usage.get("total_tokens", 0),
                )
                return response
            except Exception as e:
                logger.warning("llm_provider_failed", name=name, error=str(e))
                last_error = e
                # Continue to next fallback
                continue

        raise RuntimeError(
            f"All LLM providers failed. Last error: {last_error}"
        )
