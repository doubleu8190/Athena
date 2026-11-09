"""LLM 协议适配器。"""

from adapter.llm.openai_chat_completions import OpenAIChatCompletionsAdapter
from adapter.llm.openai_responses import OpenAIResponsesAdapter

__all__ = ["OpenAIChatCompletionsAdapter", "OpenAIResponsesAdapter"]
