"""LLM Provider abstraction layer.

Only providers with native Function Calling support are accepted.
"""

from athena.core.llm_provider.base import LLMProvider, LLMResponse
from athena.core.llm_provider.manager import LLMProviderManager
from athena.core.llm_provider.openai import OpenAIProvider
from athena.core.llm_provider.anthropic import AnthropicProvider
from athena.core.llm_provider.litellm import LiteLLMProvider

__all__ = [
    "LLMProvider",
    "LLMResponse",
    "LLMProviderManager",
    "OpenAIProvider",
    "AnthropicProvider",
    "LiteLLMProvider",
]
