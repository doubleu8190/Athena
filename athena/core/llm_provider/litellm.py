"""LiteLLM proxy provider.

Routes through a LiteLLM gateway that proxies to multiple LLM backends.
Uses the OpenAI-compatible API format.
"""

from __future__ import annotations

import os
from typing import Any

from athena.core.llm_provider.base import LLMProvider, LLMResponse
from athena.core.llm_provider.openai import OpenAIProvider
from athena.logging_config import get_logger

logger = get_logger(__name__)


class LiteLLMProvider(OpenAIProvider):
    """Provider that routes through a LiteLLM proxy gateway.

    LiteLLM exposes an OpenAI-compatible API, so we delegate to OpenAIProvider
    with the LiteLLM base URL and model routing.
    """

    def __init__(
        self,
        api_key_env: str = "LITELLM_API_KEY",
        model: str = "gpt-4o",
        base_url: str = "http://litellm:4000",
    ):
        super().__init__(
            api_key_env=api_key_env,
            model=model,
            base_url=base_url,
        )

    @property
    def provider_name(self) -> str:
        return "litellm"
