"""LLM Provider Manager.

Manages two LangChain chat models:
- **Primary model** (``default_provider``): strong model for main agent tasks
- **Fast model** (``default_summarize_provider``): cheap/fast model for summarization

Uses native LangChain ``BaseChatModel`` instances (ChatOpenAI, ChatAnthropic)
so that message format conversion is handled automatically.
"""

from __future__ import annotations

from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage

from athena.config import Config, LLMProviderConfig
from athena.logging_config import get_logger

logger = get_logger(__name__)


class LLMProviderManager:
    """Manages two LangChain chat models: primary and fast.

    Uses native LangChain ``BaseChatModel`` instances so that:
    - Message format conversion (OpenAI ↔ Anthropic) is automatic
    - ``model.bind_tools()`` works natively with ``BaseTool`` objects
    - ``AIMessage.tool_calls`` is always in the standard LangChain format
    """

    def __init__(self, config: Config) -> None:
        self._config = config
        self._default_provider = config.llm.default_provider
        self._fast_provider = config.llm.default_summarize_provider
        self._models: dict[str, BaseChatModel] = {}
        self._init_models()

    def _init_models(self) -> None:
        """Instantiate configured LangChain chat models."""
        for name, pdata in self._config.llm.providers.items():
            model = self._create_model(name, pdata)
            if model:
                self._models[name] = model
                logger.info("llm_model_registered", name=name, model=pdata.model)

    def _create_model(self, name: str, pdata: LLMProviderConfig) -> BaseChatModel | None:
        """Create a LangChain chat model instance based on configuration.

        Uses ``ChatOpenAI`` for OpenAI-compatible providers (OpenAI, DeepSeek,
        MiMo, LiteLLM) and ``ChatAnthropic`` for Anthropic providers.
        """
        from athena.core.secrets import get_secret
        api_key = get_secret(pdata.api_key_env, "") if pdata.api_key_env else ""
        if not api_key:
            logger.warning("llm_api_key_missing", name=name, env=pdata.api_key_env)
            return None

        kwargs: dict[str, Any] = {
                "model": pdata.model,
                "api_key": api_key,
                "base_url": pdata.base_url,
            }
     
        if pdata.format == "anthropic":
            from langchain_anthropic import ChatAnthropic
            return ChatAnthropic(**kwargs)
        else:
            from langchain_openai import ChatOpenAI
            return ChatOpenAI(**kwargs)

    @property
    def base_model(self) -> BaseChatModel:
        """The primary (strong) model for main agent tasks."""
        m = self._models.get(self._default_provider)
        if m is None:
            raise ValueError(
                f"Primary model '{self._default_provider}' not found or has no API key"
            )
        return m

    @property
    def fast_model(self) -> BaseChatModel | None:
        """The fast/cheap model for summarization. None if not configured."""
        if not self._fast_provider:
            return None
        return self._models.get(self._fast_provider)

    def get_model(self, name: str | None = None) -> BaseChatModel:
        """Get a model by name. Defaults to the primary model."""
        target = name or self._default_provider
        model = self._models.get(target)
        if model is None:
            raise ValueError(f"LLM provider '{target}' not found or has no API key")
        return model

    async def invoke(
        self,
        messages: list[BaseMessage],
        *,
        model_name: str | None = None,
    ) -> AIMessage:
        """Invoke the LLM.

        Accepts native LangChain messages (SystemMessage, HumanMessage,
        AIMessage, ToolMessage). LangChain handles format conversion to the
        provider's native API format automatically.

        Args:
            messages: Chat messages as LangChain ``BaseMessage`` objects.
            model_name: Explicit provider name. If ``None``, uses the default.

        Returns:
            ``AIMessage`` with ``content`` and optional ``tool_calls``.
        """
        model = self.get_model(model_name)
        response: AIMessage = await model.ainvoke(messages)

        usage = response.response_metadata.get("token_usage", {})
        logger.info(
            "llm_invoke_success",
            provider=model_name or self._default_provider,
            model=response.response_metadata.get("model", ""),
            total_tokens=usage.get("total_tokens", 0),
        )
        return response


# ── Singleton ───────────────────────────────────────────────────────────────

_llm_manager: LLMProviderManager | None = None


def get_llm_manager() -> LLMProviderManager:
    """Return the process-wide singleton LLMProviderManager (lazy-init)."""
    global _llm_manager
    if _llm_manager is not None:
        return _llm_manager

    from athena.config import get_config

    _llm_manager = LLMProviderManager(get_config())
    logger.info("llm_manager_singleton_initialized")
    return _llm_manager
