"""Configured language-model provider."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from langchain_core.language_models import BaseChatModel

from domain.integrations import LLMPort

from .options import LLMOptions, coerce_llm_options


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    attempts: int = 3
    timeout_seconds: float = 120.0
    delay_seconds: float = 0.0


class RetryingLLMProvider(LLMPort):
    """为任意异步模型提供重试、超时和 token 统计。"""

    def __init__(
        self,
        model: Any,
        *,
        policy: RetryPolicy | None = None,
        token_counter: Callable[[str], int] | None = None,
    ) -> None:
        """初始化模型适配器。

        ``model`` 是实现异步 ``invoke`` 的模型；``policy`` 控制重试和超时；
        ``token_counter`` 是无法从模型取得 tokenizer 时使用的显式计数器。
        """
        self._model = model
        self._policy = policy or RetryPolicy()
        self._token_counter = token_counter or (lambda value: len(value.split()))

    async def invoke(self, messages: list[Any], **kwargs: Any) -> Any:
        """调用底层模型并在失败时按策略重试。"""
        last_error: Exception | None = None
        for attempt in range(max(1, self._policy.attempts)):
            try:
                return await asyncio.wait_for(
                    self._model.invoke(messages, **kwargs),
                    timeout=self._policy.timeout_seconds,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last_error = exc
                if attempt + 1 < max(1, self._policy.attempts) and self._policy.delay_seconds:
                    await asyncio.sleep(self._policy.delay_seconds)
        raise RuntimeError("LLM invocation failed") from last_error

    async def complete(self, messages: list[Any], **kwargs: Any) -> Any:
        """使用 ``invoke`` 提供端口要求的 completion 别名。"""
        return await self.invoke(messages, **kwargs)

    def count_text_tokens(self, text: str) -> int:
        """用适配器配置的计数器统计文本 token 数量。"""
        return int(self._token_counter(text))


class ConfiguredLLMProvider(RetryingLLMProvider):
    """根据运行配置延迟创建远程聊天模型。"""

    def __init__(self, options: LLMOptions | Any) -> None:
        """保存模型和重试配置；首次调用或计数时才创建 SDK 模型。"""
        self._options = coerce_llm_options(options)
        self._loaded = False
        super().__init__(
            None,
            policy=RetryPolicy(
                attempts=self._options.retry.attempts,
                timeout_seconds=self._options.retry.timeout_seconds,
                delay_seconds=self._options.retry.delay_seconds,
            ),
        )

    def _load_model(self) -> BaseChatModel:
        """根据 provider 配置创建 LangChain 聊天模型。"""
        config = self._options.primary
        provider = config.provider.strip().lower()
        if provider not in {"openai", "anthropic", "ollama"}:
            raise ValueError(f"Unsupported LLM provider: {config.provider}")
        if not config.api_key and provider != "ollama":
            raise ValueError(f"LLM provider {config.provider!r} requires an API key")
        kwargs: dict[str, Any] = {"model": config.model, "api_key": config.api_key}
        if config.base_url:
            kwargs["base_url"] = config.base_url
        if config.temperature >= 0:
            kwargs["temperature"] = config.temperature
        if config.max_tokens >= 0:
            kwargs["max_tokens"] = config.max_tokens
        if provider == "openai":
            from langchain_openai import ChatOpenAI
            return ChatOpenAI(**kwargs)
        if provider == "anthropic":
            from langchain_anthropic import ChatAnthropic
            return ChatAnthropic(**kwargs)
        if provider == "ollama":
            from langchain_ollama import ChatOllama
            return ChatOllama(
                model=config.model,
                base_url=config.base_url or "http://localhost:11434",
            )
        raise AssertionError(f"Unhandled LLM provider: {config.provider}")

    def _ensure_model(self) -> BaseChatModel:
        """按需加载配置的模型并返回已缓存的实例。"""
        if not self._loaded:
            self._model = self._load_model()
            self._loaded = True
        return self._model

    async def invoke(self, messages: list[Any], **kwargs: Any) -> Any:
        """确保模型已加载后执行一次带重试的调用。"""
        self._ensure_model()
        return await super().invoke(messages, **kwargs)

    def count_text_tokens(self, text: str) -> int:
        """使用配置模型的 tokenizer 统计文本 token 数量。

        OpenAI 模型按具体模型名选择 tiktoken 编码；其他 LangChain chat
        model 使用 ``get_num_tokens`` 或 ``get_token_ids`` 接口。
        """
        config = self._options.primary
        if config.provider.strip().lower() == "openai":
            import tiktoken

            try:
                encoding = tiktoken.encoding_for_model(config.model)
            except KeyError:
                # 自定义 deployment 名称没有官方映射时使用 OpenAI 当前通用编码。
                encoding = tiktoken.get_encoding("cl100k_base")
            return len(encoding.encode(text))
        model = self._ensure_model()
        get_num_tokens = getattr(model, "get_num_tokens", None)
        if callable(get_num_tokens):
            return int(get_num_tokens(text))
        get_token_ids = getattr(model, "get_token_ids", None)
        if callable(get_token_ids):
            return len(get_token_ids(text))
        return super().count_text_tokens(text)


__all__ = ["ConfiguredLLMProvider", "RetryPolicy", "RetryingLLMProvider"]
