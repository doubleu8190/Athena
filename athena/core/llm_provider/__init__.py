"""LLM Provider abstraction layer.

Uses LangChain ``BaseChatModel`` instances (ChatOpenAI, ChatAnthropic) for
automatic message format conversion via ``LLMProviderManager``.
"""

from athena.core.llm_provider.manager import LLMProviderManager, get_llm_manager

__all__ = [
    "LLMProviderManager",
    "get_llm_manager",
]
