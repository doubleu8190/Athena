"""DeepSeek LLM provider.

DeepSeek API is OpenAI-compatible, so we extend OpenAIProvider with
DeepSeek-specific defaults (base URL, API key env, provider name).
"""

from __future__ import annotations

from athena.core.llm_provider.openai import OpenAIProvider
from athena.logging_config import get_logger

logger = get_logger(__name__)


class DeepSeekProvider(OpenAIProvider):
    """Provider for DeepSeek models via OpenAI-compatible API.

    Supports: deepseek-v4-pro, deepseek-v4-flash, and other DeepSeek models
    with native Function Calling capability.
    """

    def __init__(
        self,
        api_key_env: str = "DEEPSEEK_API_KEY",
        model: str = "deepseek-v4-pro",
        base_url: str = "https://api.deepseek.com/v1",
    ):
        super().__init__(
            api_key_env=api_key_env,
            model=model,
            base_url=base_url,
        )

    @property
    def provider_name(self) -> str:
        return "deepseek"
