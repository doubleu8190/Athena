"""MiMo LLM provider.

MiMo API is OpenAI-compatible, so we extend OpenAIProvider with
MiMo-specific defaults (base URL, API key env, provider name).
"""

from __future__ import annotations

from athena.core.llm_provider.openai import OpenAIProvider
from athena.logging_config import get_logger

logger = get_logger(__name__)


class MimoProvider(OpenAIProvider):
    """Provider for Xiaomi MiMo models via OpenAI-compatible API.

    Supports: mimo-v2.5-pro, and other MiMo models
    with native Function Calling capability.
    """

    def __init__(
        self,
        api_key_env: str = "MIMO_API_KEY",
        model: str = "mimo-v2.5-pro",
        base_url: str = "https://api.xiaomimimo.com/v1",
    ) -> None:
        super().__init__(
            api_key_env=api_key_env,
            model=model,
            base_url=base_url,
        )

    @property
    def provider_name(self) -> str:
        return "mimo"
