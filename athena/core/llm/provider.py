"""LLM 抽象层 — 多 Provider 配置化切换，避免厂商锁定.

支持 OpenAI / Anthropic / DeepSeek / Ollama，所有 Provider 以 streaming=True 初始化。
统一接口：ainvoke / astream / bind_tools / with_structured_output。
集成 LLMRetryManager 实现指数退避重试与多级故障转移。
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, Protocol, runtime_checkable

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage

from athena.config.settings import Settings, get_settings
from athena.core.llm.retry import ErrorCategory, LLMRetryManager, RetryConfig, RetryResult
from athena.utils.logging import get_logger

logger = get_logger(__name__)


@runtime_checkable
class LLMProviderProtocol(Protocol):
    """统一的 LLM 调用接口协议."""

    @property
    def model(self) -> BaseChatModel: ...

    async def ainvoke(self, messages: list[BaseMessage], **kwargs: Any) -> Any: ...

    def astream(self, messages: list[BaseMessage], **kwargs: Any) -> AsyncIterator[Any]: ...

    def bind_tools(self, tools: list[Any]) -> "LLMProviderProtocol": ...

    def with_structured_output(self, schema: type) -> Any: ...


class LLMProvider:
    """LLM Provider 封装层.

    将 LangChain 的 BaseChatModel 包装为统一接口，
    支持 bind_tools 链式调用、结构化输出、以及指数退避重试。
    """

    def __init__(
        self,
        model: Any,
        retry_manager: LLMRetryManager | None = None,
    ) -> None:
        self._model = model
        self._retry_manager = retry_manager

    @property
    def model(self) -> Any:
        """获取底层 LangChain 模型实例."""
        return self._model

    def set_retry_manager(self, retry_manager: LLMRetryManager) -> None:
        """注入重试管理器."""
        self._retry_manager = retry_manager

    async def ainvoke(self, messages: list[BaseMessage], **kwargs: Any) -> Any:
        """异步调用模型，失败时使用指数退避重试."""
        if self._retry_manager is None:
            return await self._model.ainvoke(messages, **kwargs)

        result = await self._retry_manager.execute_with_retry(
            self._model.ainvoke, messages, **kwargs,
        )
        if not result.success:
            error = result.error or RuntimeError("Unknown LLM error")
            if result.error_category in (ErrorCategory.CONTEXT_OVERFLOW,):
                raise ValueError(
                    f"上下文超限 ({result.attempts} 次尝试)，需压缩后重试"
                ) from error
            if result.error_category == ErrorCategory.PERMANENT:
                raise error
            raise RuntimeError(
                f"LLM 调用失败 ({result.attempts} 次尝试): {error}"
            ) from error
        return result.result

    def astream(self, messages: list[BaseMessage], **kwargs: Any) -> AsyncIterator[Any]:
        """流式调用模型，返回带重试能力的 async generator."""
        return self._retry_astream(messages, **kwargs)

    async def _retry_astream(
        self, messages: list[BaseMessage], **kwargs: Any
    ) -> AsyncIterator[Any]:
        """带重试的流式输出.

        当 astream 创建 generator 失败时进行重试；
        迭代过程中的异常由调用方（Harness）处理。
        """
        if self._retry_manager is None:
            gen = self._model.astream(messages, **kwargs)
            async for chunk in gen:
                yield chunk
            return

        # 重试 generator 创建
        last_error: Exception | None = None
        config = RetryConfig(max_attempts=3, min_delay_ms=1000, max_delay_ms=10000)

        for attempt in range(config.max_attempts):
            try:
                gen = self._model.astream(messages, **kwargs)
                async for chunk in gen:
                    yield chunk
                return  # 正常结束
            except Exception as e:
                last_error = e
                category = ErrorCategory.TRANSIENT
                from athena.core.llm.retry import categorize_error
                category = categorize_error(e)

                if attempt < config.max_attempts - 1 and category != ErrorCategory.PERMANENT:
                    delay_ms = config.min_delay_ms * (2 ** attempt)
                    delay_s = min(delay_ms, config.max_delay_ms) / 1000
                    logger.warning(
                        "llm_astream_retry",
                        attempt=attempt + 1,
                        error=str(e),
                        category=category,
                    )
                    await asyncio.sleep(delay_s)
                    continue
                raise

        if last_error:
            raise last_error

    def bind_tools(self, tools: list[Any]) -> "LLMProvider":
        """绑定工具到模型，返回新的 provider 实例."""
        bound = self._model.bind_tools(tools)
        return LLMProvider(bound, retry_manager=self._retry_manager)

    def with_structured_output(self, schema: type) -> Any:
        """绑定结构化输出 schema，返回可调用的 runnable."""
        return self._model.with_structured_output(schema)

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> "LLMProvider":
        """根据配置创建 Provider 实例."""
        settings = settings or get_settings()
        model = _create_chat_model(settings)
        retry_config = RetryConfig(
            max_attempts=3,
            min_delay_ms=2000,
            max_delay_ms=30000,
            jitter=0.1,
            timeout_ms=60000,
        )
        retry_manager = LLMRetryManager(retry_config=retry_config)
        return cls(model, retry_manager=retry_manager)


def _create_chat_model(settings: Settings) -> BaseChatModel:
    """根据配置创建对应的 LangChain ChatModel，统一以 streaming=True 初始化."""
    provider = settings.llm_provider.lower()
    common_kwargs: dict[str, Any] = {
        "temperature": settings.llm_temperature,
        "max_tokens": settings.llm_max_tokens,
        "streaming": True,
    }

    if provider == "openai":
        from langchain_openai import ChatOpenAI

        kwargs: dict[str, Any] = {
            "model": settings.llm_model,
            "api_key": settings.llm_api_key,
            **common_kwargs,
        }
        if settings.llm_base_url:
            kwargs["base_url"] = settings.llm_base_url
        return ChatOpenAI(**kwargs)

    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        kwargs = {
            "model": settings.llm_model,
            "api_key": settings.llm_api_key,
            **common_kwargs,
        }
        if settings.llm_base_url:
            kwargs["base_url"] = settings.llm_base_url
        return ChatAnthropic(**kwargs)

    if provider == "deepseek":
        # DeepSeek 兼容 OpenAI 接口
        from langchain_openai import ChatOpenAI

        kwargs = {
            "model": settings.llm_model,
            "api_key": settings.llm_api_key,
            "base_url": settings.llm_base_url or "https://api.deepseek.com",
            **common_kwargs,
        }
        return ChatOpenAI(**kwargs)

    if provider == "ollama":
        from langchain_ollama import ChatOllama

        kwargs: dict[str, Any] = {
            "model": settings.llm_model,
            **common_kwargs,
        }
        if settings.llm_base_url:
            kwargs["base_url"] = settings.llm_base_url
        return ChatOllama(**kwargs)

    raise ValueError(f"Unsupported LLM provider: {provider}")


# 全局单例
_llm_instance: LLMProvider | None = None


def get_llm_provider(settings: Settings | None = None) -> LLMProvider:
    """获取 LLM Provider 单例."""
    global _llm_instance
    if _llm_instance is None:
        _llm_instance = LLMProvider.from_settings(settings)
        logger.info("llm_provider_initialized", provider=get_settings().llm_provider)
    return _llm_instance


def reset_llm_provider() -> None:
    """重置单例（测试用）."""
    global _llm_instance
    _llm_instance = None
