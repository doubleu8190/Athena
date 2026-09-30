"""LLM 抽象层 — 多 Provider 配置化切换，避免厂商锁定.

支持 OpenAI / Anthropic / DeepSeek / Ollama。
统一接口：ainvoke / bind_tools / with_structured_output。
集成 LLMRetryManager 实现指数退避重试与多级故障转移。
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import (
    Any,
    Awaitable,
    Callable,
    Protocol,
    TypeVar,
    cast,
    runtime_checkable,
)

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage
from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel

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
from athena.observability.langsmith import finish_span, trace_span

logger = get_logger(__name__)

ModelT = TypeVar("ModelT", bound=BaseModel)


@runtime_checkable
class LLMProviderProtocol(Protocol):
    """统一的 LLM 调用接口协议."""

    @property
    def model(self) -> BaseChatModel:
        """返回底层 LangChain 聊天模型。"""
        ...

    async def ainvoke(self, messages: list[BaseMessage], **kwargs: Any) -> BaseMessage:
        """异步调用模型并返回消息结果。"""
        ...

    def bind_tools(self, tools: list[BaseTool]) -> "LLMProviderProtocol":
        """返回绑定指定工具定义的模型提供商。"""
        ...

    async def ainvoke_structured(
        self, schema: type[ModelT], messages: list[BaseMessage], **kwargs: Any
    ) -> ModelT:
        """异步调用模型并返回通过 schema 校验的结构化结果。"""
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
        """

        参数：
            model (BaseChatModel): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。
            retry_manager (LLMRetryManager | None): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。

        返回值：
            None: 返回该方法声明类型的业务结果，内容由方法职责确定。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
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
        result = await self._invoke_with_retry(self._model.ainvoke, messages, **kwargs)
        return cast(BaseMessage, result)

    async def ainvoke_structured(
        self,
        schema: type[ModelT],
        messages: list[BaseMessage],
        **kwargs: Any,
    ) -> ModelT:
        """调用模型并将结果校验为指定的 Pydantic 模型。

        结构化调用与普通调用使用相同的重试和故障转移策略；故障转移时会在
        备用 Provider 上重新绑定相同 schema，避免退化为未约束的文本调用。
        """
        runnable = self._model.with_structured_output(schema)

        async def invoke_fallback(fallback: LLMProvider) -> ModelT:
            return await fallback.ainvoke_structured(schema, messages, **kwargs)

        result = await self._invoke_with_retry(
            runnable.ainvoke,
            messages,
            fallback_invoke=invoke_fallback,
            **kwargs,
        )
        if isinstance(result, schema):
            return result
        if isinstance(result, BaseModel):
            return schema.model_validate(result.model_dump(mode="json"))
        if isinstance(result, dict):
            return schema.model_validate(result)
        raise ValueError(
            f"structured LLM returned unsupported type: {type(result).__name__}"
        )

    async def _invoke_with_retry(
        self,
        invoke: Callable[..., Awaitable[Any]],
        *args: Any,
        fallback_invoke: Callable[["LLMProvider"], Awaitable[Any]] | None = None,
        **kwargs: Any,
    ) -> Any:
        """通过当前 Provider 的重试与故障转移策略执行一次模型调用。"""
        if self._retry_manager is None:
            return await invoke(*args, **kwargs)

        result = await self._retry_manager.execute_with_retry(
            invoke,
            *args,
            fallback_invoke=fallback_invoke,
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

    def bind_tools(self, tools: list[StructuredTool]) -> LLMProvider:
        """绑定工具到模型，返回新的 provider 实例（不可变）.

        LangChain 的 bind_tools/bind 是不可变 API：返回新的 RunnableBinding，
        不修改原模型。必须接住返回值并包装绑定后的模型，否则工具定义永远不会
        到达模型（静默失效）。共享同一 LLMProvider 时每次调用各得独立 binding，
        互不污染；严禁原地改写 self._model（并发 run 绑定不同工具集会竞态）。
        """
        # LangChain 把 bind_tools 返回类型标注为 Runnable（运行期实为
        # _ChatModelBinding），但其方法面（ainvoke/bind_tools/
        # with_structured_output）与 BaseChatModel 一致，故 cast 收窄仅为
        # 消除类型标注差异，运行期安全。
        bound_model = cast(BaseChatModel, self._model.bind_tools(tools))
        provider = LLMProvider(bound_model, retry_manager=self._retry_manager)
        provider._token_counter = self._token_counter
        return provider

    @property
    def fallback_providers(self) -> list["LLMProvider"]:
        """返回重试管理器注册的备用模型列表。"""
        if self._retry_manager is None:
            return []
        return list(self._retry_manager._fallback_providers)

    def supports_structured_output(self, schema: type) -> bool:
        """探测底层模型是否能为指定 schema 提供原生结构化输出。

        参数：
            schema: Pydantic 模型或 JSON Schema。

        返回值：
            bool: 模型实现并成功构造结构化 Runnable 时返回 True。

        异常：
            不主动抛出业务异常；探测失败统一视为不支持。
        """
        try:
            self._model.with_structured_output(schema)
        except (NotImplementedError, ValueError, TypeError):
            return False
        return True

    @classmethod
    def from_config(
        cls,
        config: LLMProviderConfig,
        settings: Settings,
        *,
        retry_settings: LLMRetrySettings | None = None,
    ) -> "LLMProvider":
        """根据单个配置创建统一的 LLM Provider。

        用于一次性连通性测试等不应直接接触底层 ChatModel 的入口。未指定
        ``retry_settings`` 时使用全局主 Provider 的重试配置。
        """
        retry_config = _to_retry_config(retry_settings or settings.llm_retry)
        return cls(
            _create_chat_model(config, settings),
            retry_manager=LLMRetryManager(retry_config=retry_config),
        )

    @classmethod
    def from_primary_settings(cls, settings: Settings) -> "LLMProvider":
        """根据配置创建主 Provider和副 Provider，并将 fallback 注入重试链.

        创建顺序：
        1. 用 primary config 创建主 provider
        2. 用 secondary config 之后的 config 创建 fallback providers
        3. 将 fallback providers 注入 retry_manager 的故障转移链
        """
        primary_config = settings.primary_llm
        provider = cls.from_config(
            primary_config,
            settings,
            retry_settings=settings.llm_retry,
        )
        retry_manager = provider._retry_manager
        if retry_manager is None:
            raise RuntimeError("primary LLM provider must have a retry manager")

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

        return provider

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

        logger.info(
            "llm_secondary_created",
            name=secondary_config.name,
            provider=secondary_config.provider,
            model=secondary_config.model,
        )
        return cls.from_config(
            secondary_config,
            settings,
            retry_settings=settings.llm_secondary_retry,
        )


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
    # ChatOllama 使用 num_predict（且无 streaming 字段）。
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
