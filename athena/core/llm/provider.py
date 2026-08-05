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
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool, StructuredTool

from athena.config.settings import LLMProviderConfig, Settings, get_settings
from athena.core.llm.retry import (
    ErrorCategory,
    LLMRetryManager,
    RetryConfig,
    RetryResult,
)
from athena.utils.logging import get_logger

logger = get_logger(__name__)


@runtime_checkable
class LLMProviderProtocol(Protocol):
    """统一的 LLM 调用接口协议."""

    @property
    def model(self) -> BaseChatModel: ...

    async def ainvoke(
        self, messages: list[BaseMessage], **kwargs: Any
    ) -> BaseMessage: ...

    def astream(
        self, messages: list[BaseMessage], **kwargs: Any
    ) -> AsyncIterator[BaseMessage]: ...

    def bind_tools(self, tools: list[BaseTool]) -> "LLMProviderProtocol": ...

    def with_structured_output(self, schema: type) -> Runnable: ...


class LLMProvider:
    """LLM Provider 封装层.

    将 LangChain 的 BaseChatModel 包装为统一接口，
    支持 bind_tools 链式调用、结构化输出、以及指数退避重试。
    """

    def __init__(
        self,
        model: BaseChatModel,
        retry_manager: LLMRetryManager | None = None,
    ) -> None:
        self._model = model
        self._retry_manager = retry_manager

    @property
    def model(self) -> BaseChatModel:
        """获取底层 LangChain 模型实例."""
        return self._model

    def set_retry_manager(self, retry_manager: LLMRetryManager) -> None:
        """注入重试管理器."""
        self._retry_manager = retry_manager

    async def ainvoke(self, messages: list[BaseMessage], **kwargs: Any) -> BaseMessage:
        """异步调用模型，失败时使用指数退避重试."""
        if self._retry_manager is None:
            return await self._model.ainvoke(messages, **kwargs)

        result = await self._retry_manager.execute_with_retry(
            self._model.ainvoke,
            messages,
            **kwargs,
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
        if result.result is None:
            raise RuntimeError("LLM returned None result despite success")
        return result.result

    def astream(
        self, messages: list[BaseMessage], **kwargs: Any
    ) -> AsyncIterator[BaseMessage]:
        """流式调用模型，返回带重试能力的 async generator."""
        return self._retry_astream(messages, **kwargs)

    async def _retry_astream(
        self, messages: list[BaseMessage], **kwargs: Any
    ) -> AsyncIterator[BaseMessage]:
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

                if (
                    attempt < config.max_attempts - 1
                    and category != ErrorCategory.PERMANENT
                ):
                    delay_ms = config.min_delay_ms * (2**attempt)
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

    def bind_tools(self, tools: list[StructuredTool]) -> LLMProvider:
        """绑定工具到模型，返回新的 provider 实例."""
        self._model.bind_tools(tools)
        return LLMProvider(self._model, retry_manager=self._retry_manager)

    def with_structured_output(self, schema: type) -> Runnable:
        """绑定结构化输出 schema，返回可调用的 runnable."""
        return self._model.with_structured_output(schema)

    @classmethod
    def from_settings(cls, settings: Settings) -> "LLMProvider":
        """根据配置创建主 Provider，并将 secondary/fallback 注入重试链.

        创建顺序：
        1. 用 primary config 创建主 provider
        2. 用 secondary 及之后的 config 创建 fallback providers
        3. 将 fallback providers 注入 retry_manager 的故障转移链
        """
        primary_config = settings.primary_llm
        model = _create_chat_model(primary_config, settings)
        retry_config = RetryConfig(
            max_attempts=3,
            min_delay_ms=2000,
            max_delay_ms=30000,
            jitter=0.1,
            timeout_ms=60000,
        )
        retry_manager = LLMRetryManager(retry_config=retry_config)

        # 注入 secondary / fallback providers 到故障转移链
        for fallback_config in settings.fallback_llm_list:
            fallback_model = _create_chat_model(fallback_config, settings)
            fallback_provider = cls(fallback_model)
            retry_manager.add_fallback_provider(fallback_provider)
            logger.info(
                "llm_fallback_registered",
                name=fallback_config.name,
                provider=fallback_config.provider,
                model=fallback_config.model,
            )

        return cls(model, retry_manager=retry_manager)


def _create_chat_model(
    config: LLMProviderConfig, settings: Settings
) -> BaseChatModel:
    """根据 LLMProviderConfig 创建对应的 LangChain ChatModel.

    provider 级别的 temperature / max_tokens 为 -1 时回退到全局默认值。
    """
    provider = config.provider.lower()
    temperature = config.temperature if config.temperature >= 0 else settings.llm_temperature
    max_tokens = config.max_tokens if config.max_tokens >= 0 else settings.llm_max_tokens

    common_kwargs: dict[str, Any] = {
        "temperature": temperature,
        "max_tokens": max_tokens,
        "streaming": True,
    }

    if provider == "openai":
        from langchain_openai import ChatOpenAI

        kwargs: dict[str, Any] = {
            "model": config.model,
            "api_key": config.api_key,
            **common_kwargs,
        }
        if config.base_url:
            kwargs["base_url"] = config.base_url
        return ChatOpenAI(**kwargs)

    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        kwargs = {
            "model": config.model,
            "api_key": config.api_key,
            **common_kwargs,
        }
        if config.base_url:
            kwargs["base_url"] = config.base_url
        return ChatAnthropic(**kwargs)

    if provider == "ollama":
        from langchain_ollama import ChatOllama

        kwargs: dict[str, Any] = {
            "model": config.model,
            **common_kwargs,
        }
        if config.base_url:
            kwargs["base_url"] = config.base_url
        return ChatOllama(**kwargs)

    raise ValueError(f"Unsupported LLM provider: {provider}")


# 全局单例
_llm_instance: LLMProvider


def get_llm_provider() -> LLMProvider:
    """获取 LLM Provider 单例."""
    return _llm_instance

def set_llm_provider(provider: LLMProvider) -> None:
    """设置全局 LLM Provider 实例（测试用）."""
    global _llm_instance
    _llm_instance = provider

