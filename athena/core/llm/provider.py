"""LLM 抽象层 — 多 Provider 配置化切换，避免厂商锁定.

支持 OpenAI / Anthropic / DeepSeek / Ollama，所有 Provider 以 streaming=True 初始化。
统一接口：ainvoke / astream / bind_tools / with_structured_output。
集成 LLMRetryManager 实现指数退避重试与多级故障转移。
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any, AsyncIterator, Protocol, cast, runtime_checkable

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool, StructuredTool

from athena.config.settings import (
    LLMProviderConfig,
    LLMRetrySettings,
    Settings,
)
from athena.core.llm.retry import (
    ErrorCategory,
    LLMRetryManager,
    RetryConfig,
)
from athena.core.llm.tokens import ModelTokenCounter, TokenCounter
from athena.utils.logging import get_logger

logger = get_logger(__name__)


@runtime_checkable
class LLMProviderProtocol(Protocol):
    """统一的 LLM 调用接口协议."""

    @property
    def model(self) -> BaseChatModel:
        """返回底层 LangChain 聊天模型。"""
        ...

    async def ainvoke(
        self, messages: list[BaseMessage], **kwargs: Any
    ) -> BaseMessage:
        """异步调用模型并返回消息结果。"""
        ...

    def astream(
        self, messages: list[BaseMessage], **kwargs: Any
    ) -> AsyncIterator[BaseMessage]:
        """以异步迭代器流式返回模型消息块。"""
        ...

    def bind_tools(self, tools: list[BaseTool]) -> "LLMProviderProtocol":
        """返回绑定指定工具定义的模型提供商。"""
        ...

    def with_structured_output(self, schema: type) -> Runnable:
        """返回约束为指定结构化输出的模型提供商。"""
        ...


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
        """初始化当前对象。

        参数：
            model (BaseChatModel): 输入参数；其类型和取值约束由方法签名及实现定义。
            retry_manager (LLMRetryManager | None): 输入参数；其类型和取值约束由方法签名及实现定义。

        返回值：
            None: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        self._model = model
        self._retry_manager = retry_manager
        self._token_counter: TokenCounter = ModelTokenCounter(model)

    @property
    def model(self) -> BaseChatModel:
        """获取底层 LangChain 模型实例."""
        return self._model

    def set_retry_manager(self, retry_manager: LLMRetryManager) -> None:
        """注入重试管理器."""
        self._retry_manager = retry_manager

    def count_text_tokens(self, text: str) -> int:
        """使用可用的最佳本地 tokenizer 统计原始文本 token 数。"""
        return self._token_counter.count_text_tokens(text)

    def count_message_tokens(self, messages: Sequence[BaseMessage]) -> int:
        """不发起额外 API 请求，统计结构化消息的 token 数。"""
        return self._token_counter.count_message_tokens(messages)

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
        config = self._retry_manager.config

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
        """绑定工具到模型，返回新的 provider 实例（不可变）.

        LangChain 的 bind_tools/bind 是不可变 API：返回新的 RunnableBinding，
        不修改原模型。必须接住返回值并包装绑定后的模型，否则工具定义永远不会
        到达模型（静默失效）。共享同一 LLMProvider 时每次调用各得独立 binding，
        互不污染；严禁原地改写 self._model（并发 run 绑定不同工具集会竞态）。
        """
        # LangChain 把 bind_tools 返回类型标注为 Runnable（运行期实为
        # _ChatModelBinding），但其方法面（ainvoke/astream/bind_tools/
        # with_structured_output）与 BaseChatModel 一致，故 cast 收窄仅为
        # 消除类型标注差异，运行期安全。
        bound_model = cast(BaseChatModel, self._model.bind_tools(tools))
        provider = LLMProvider(bound_model, retry_manager=self._retry_manager)
        provider._token_counter = self._token_counter
        return provider

    def with_structured_output(self, schema: type) -> Runnable:
        """绑定结构化输出 schema，返回可调用的 runnable."""
        return self._model.with_structured_output(schema)

    @classmethod
    def from_primary_settings(cls, settings: Settings) -> "LLMProvider":
        """根据配置创建主 Provider和副 Provider，并将 fallback 注入重试链.

        创建顺序：
        1. 用 primary config 创建主 provider
        2. 用 secondary config 之后的 config 创建 fallback providers
        3. 将 fallback providers 注入 retry_manager 的故障转移链
        """
        primary_config = settings.primary_llm
        model = _create_chat_model(primary_config, settings)
        retry_config = _to_retry_config(settings.llm_retry)
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

    @classmethod
    def from_secondary_settings(cls, settings: Settings) -> "LLMProvider | None":
        """根据配置创建副 Provider（用于检索、摘要等轻量任务）.

        优先使用 secondary 配置；若未配置 secondary 则回退到 fallback；
        若只配了一个 provider 则返回 None（调用方应使用主 provider）。
        """
        secondary_config = settings.secondary_llm
        if secondary_config is None:
            fallback_list = settings.fallback_llm_list
            secondary_config = fallback_list[0] if fallback_list else None
        if secondary_config is None:
            return None

        model = _create_chat_model(secondary_config, settings)
        retry_config = _to_retry_config(settings.llm_secondary_retry)
        retry_manager = LLMRetryManager(retry_config=retry_config)
        logger.info(
            "llm_secondary_created",
            name=secondary_config.name,
            provider=secondary_config.provider,
            model=secondary_config.model,
        )
        return cls(model, retry_manager=retry_manager)


def _to_retry_config(settings: LLMRetrySettings) -> RetryConfig:
    """将外部配置模型转换为 LLM 重试领域配置."""
    return RetryConfig(
        max_attempts=settings.max_attempts,
        min_delay_ms=settings.min_delay_ms,
        max_delay_ms=settings.max_delay_ms,
        jitter=settings.jitter,
        timeout_ms=settings.timeout_ms,
    )


def _create_chat_model(config: LLMProviderConfig, settings: Settings) -> BaseChatModel:
    """根据 LLMProviderConfig 创建对应的 LangChain ChatModel.

    provider 级别的 temperature / max_tokens 为 -1 时回退到全局默认值。
    """
    provider = config.provider.lower()
    temperature = (
        config.temperature if config.temperature >= 0 else settings.llm_temperature
    )
    max_tokens = (
        config.max_tokens if config.max_tokens >= 0 else settings.llm_max_tokens
    )

    # 注意：各 Provider 的参数名不一致。max_tokens 只对 openai/anthropic 有效；
    # ChatOllama 用 num_predict（且无 streaming 字段，流式由 .astream() 方法控制），
    # 直接传 max_tokens/streaming 会被 pydantic 静默忽略，导致 max_tokens 配置不生效。
    if provider == "openai":
        from langchain_openai import ChatOpenAI

        kwargs: dict[str, Any] = {
            "model": config.model,
            "api_key": config.api_key,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "streaming": True,
            "stream_usage": True,
        }
        if config.base_url:
            kwargs["base_url"] = config.base_url
        return ChatOpenAI(**kwargs)

    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        kwargs = {
            "model": config.model,
            "api_key": config.api_key,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "streaming": True,
        }
        if config.base_url:
            kwargs["base_url"] = config.base_url
        return ChatAnthropic(**kwargs)

    if provider == "ollama":
        from langchain_ollama import ChatOllama

        kwargs = {
            "model": config.model,
            "temperature": temperature,
            "num_predict": max_tokens,
        }
        if config.base_url:
            kwargs["base_url"] = config.base_url
        return ChatOllama(**kwargs)

    raise ValueError(f"Unsupported LLM provider: {provider}")
