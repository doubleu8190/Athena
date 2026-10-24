"""Configured language-model provider."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from typing import Any, Literal

from langchain_core.language_models import BaseChatModel
from langchain_core.runnables import Runnable

from domain.integrations import (
    LLMPort,
    LLMStreamEnd,
    LLMStreamEvent,
    LLMTextDelta,
    StructuredOutputSchema,
)
from shared import get_logger

from .options import LLMOptions, RetryOptions, coerce_llm_options

logger = get_logger(__name__)


async def _invoke_with_retry(
    model: Runnable[Any, Any],
    policy: RetryOptions,
    messages: list[Any],
    **kwargs: Any,
) -> Any:
    """按统一策略重试模型调用。

    参数：``model``（``Runnable[Any, Any]``）是模型调用对象，
    ``policy``（``RetryOptions``）控制重试次数、超时和间隔，
    ``messages``（``list[Any]``）是输入消息，
    ``kwargs`` 是模型调用参数。
    返回：底层模型的响应。
    异常：重试耗尽时抛出 ``RuntimeError``；取消时继续抛出
    ``asyncio.CancelledError``。
    """
    last_error: Exception | None = None
    for attempt in range(max(1, policy.attempts)):
        try:
            return await asyncio.wait_for(
                model.ainvoke(messages, **kwargs),
                timeout=policy.timeout_seconds,
            )
        except asyncio.CancelledError:
            logger.info("LLM invocation cancelled")
            raise
        except Exception as exc:
            last_error = exc
            if attempt + 1 < max(1, policy.attempts) and policy.delay_seconds:
                await asyncio.sleep(policy.delay_seconds)
    raise RuntimeError("LLM invocation failed") from last_error


def _create_model(options: LLMOptions, *, sdk_max_retries: int | None = None) -> BaseChatModel:
    """根据 provider 配置创建 LangChain 聊天模型。

    参数：``options``（``LLMOptions``）提供主模型配置；sdk_max_retries
    可在构造 OpenAI/Anthropic 实际 SDK 客户端时覆盖内部重试次数。
    返回：已构造的 LangChain 聊天模型。
    异常：provider 不受支持或缺少必要凭据时抛出 ``ValueError``。
    """
    config = options.primary
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
    if sdk_max_retries is not None and provider in {"openai", "anthropic"}:
        kwargs["max_retries"] = sdk_max_retries
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


def _has_model_specific_tokenizer(model: BaseChatModel) -> bool:
    """判断模型是否显式提供了专用 tokenizer。

    参数：``model``（``BaseChatModel``）是已构造的 LangChain 聊天模型。
    返回：模型自定义 token ID 或覆盖基类 tokenizer 方法时返回 ``True``。
    """
    model_type = type(model)
    return (
        model.custom_get_token_ids is not None
        or model_type.get_num_tokens is not BaseChatModel.get_num_tokens
        or model_type.get_token_ids is not BaseChatModel.get_token_ids
    )


class ConfiguredLLMProvider(LLMPort):
    """根据运行配置延迟创建远程聊天模型。"""

    def __init__(self, options: LLMOptions | Any) -> None:
        """保存配置并延迟创建 SDK 模型。

        参数：``options``（``LLMOptions`` 或运行配置对象）提供模型与重试设置。
        返回：无。
        异常：选项无法转换时由配置转换函数抛出异常。
        """
        self._options = coerce_llm_options(options)
        self._model = _create_model(self._options)
        # model_copy 不会重建内部 SDK 客户端；必须在构造时关闭流式重试。
        self._stream_model = _create_model(self._options, sdk_max_retries=0)
        self._policy = self._options.retry

    async def invoke(self, messages: list[Any], **kwargs: Any) -> Any:
        """在线程中按需加载模型，再执行带重试的调用。

        参数：``messages``（``list[Any]``）是输入消息，``kwargs`` 是模型调用参数。
        返回：底层模型的响应。
        异常：模型加载失败或重试耗尽时抛出异常。
        """
        return await _invoke_with_retry(self._model, self._policy, messages, **kwargs)

    async def invoke_structured(
        self,
        messages: list[Any],
        schema: StructuredOutputSchema,
        **kwargs: Any,
    ) -> Any:
        """按统一重试策略调用 LangChain 结构化输出 Runnable。

        参数：``messages``（``list[Any]``）是模型输入，``schema`` 是 Pydantic
        模型或 JSON Schema，``kwargs`` 是结构化 Runnable 的调用参数。
        返回：Pydantic schema 对应的模型实例，或 JSON Schema 对应的字典。
        异常：重试耗尽时抛出 ``RuntimeError``；取消时继续抛出
        ``asyncio.CancelledError``。
        """
        structured_model = self._model.with_structured_output(
            dict(schema) if isinstance(schema, Mapping) else schema,
            include_raw=False,
        )
        return await _invoke_with_retry(
            structured_model,
            self._policy,
            messages,
            **kwargs,
        )

    async def complete(self, messages: list[Any], **kwargs: Any) -> Any:
        """通过 ``invoke`` 完成端口要求的调用。

        参数：``messages`` 是输入消息，``kwargs`` 是模型调用参数。
        返回：底层模型的响应。
        异常：重试耗尽时抛出 ``RuntimeError``。
        """
        return await self.invoke(messages, **kwargs)

    def stream(self, messages: list[Any], **kwargs: Any) -> AsyncIterator[LLMStreamEvent]:
        """以供应商无关事件读取模型文本，首个文本前按统一策略重试。"""
        return self._stream_with_retry(messages, **kwargs)

    async def _stream_with_retry(
        self, messages: list[Any], **kwargs: Any
    ) -> AsyncIterator[LLMStreamEvent]:
        """流式调用仅在对外产生正文前重试，并始终关闭底层迭代器。"""
        last_error: Exception | None = None
        for attempt in range(max(1, self._policy.attempts)):
            # OpenAI/Anthropic LangChain wrappers expose max_retries; use a
            # streaming-only copy so SDK retries cannot restart visible output.
            iterator = self._stream_model.astream(messages, **kwargs).__aiter__()
            emitted_text = False
            finish_reason: Literal["stop", "length", "filtered", "unknown"] = "unknown"
            try:
                while True:
                    try:
                        chunk = await asyncio.wait_for(
                            anext(iterator), timeout=self._policy.timeout_seconds
                        )
                    except StopAsyncIteration:
                        break
                    except asyncio.CancelledError:
                        raise
                    except Exception as error:
                        if emitted_text or attempt + 1 >= max(1, self._policy.attempts):
                            raise
                        last_error = error
                        break
                    text, chunk_finish, has_tool_call = _stream_chunk_parts(chunk)
                    if has_tool_call:
                        raise RuntimeError("llm_answer_stream_returned_tool_call")
                    if chunk_finish is not None:
                        finish_reason = _normalize_finish_reason(chunk_finish)
                    if text:
                        emitted_text = True
                        yield LLMTextDelta(text)
                else:
                    pass
                if last_error is not None and not emitted_text:
                    if self._policy.delay_seconds:
                        await asyncio.sleep(self._policy.delay_seconds)
                    last_error = None
                    continue
                yield LLMStreamEnd(finish_reason)
                return
            except asyncio.CancelledError:
                logger.info("LLM streaming invocation cancelled")
                raise
            except Exception as error:
                last_error = error
                if emitted_text or attempt + 1 >= max(1, self._policy.attempts):
                    raise RuntimeError("LLM streaming invocation failed") from error
                if self._policy.delay_seconds:
                    await asyncio.sleep(self._policy.delay_seconds)
            finally:
                close = getattr(iterator, "aclose", None)
                if close is not None:
                    try:
                        await close()
                    except Exception:
                        logger.debug("LLM stream close failed", exc_info=True)
        raise RuntimeError("LLM streaming invocation failed") from last_error

    def count_text_tokens(self, text: str) -> int:
        """使用配置模型的 tokenizer 统计文本 token 数量。

        OpenAI 已知模型按名称选择 tiktoken 编码。其他模型只有显式覆盖
        tokenizer 时使用模型计数；否则返回中英混合文本的估算值。
        """
        config = self._options.primary
        if config.provider.strip().lower() == "openai":
            import tiktoken

            try:
                encoding = tiktoken.encoding_for_model(config.model)
            except Exception:
                # 未知模型别名或本地缺少编码数据时，避免误用其他模型的 tokenizer。
                return _offline_token_estimate(text)
            return len(encoding.encode(text))
        model = self._model
        if _has_model_specific_tokenizer(model):
            try:
                return int(model.get_num_tokens(text))
            except Exception:
                return _offline_token_estimate(text)
        return _offline_token_estimate(text)


def _offline_token_estimate(text: str) -> int:
    """Estimate mixed Chinese/Latin tokens without downloading tokenizer data."""
    chinese_count = 0
    count = 0
    latin_run = False
    for char in text:
        if "\u4e00" <= char <= "\u9fff":
            chinese_count += 1
            latin_run = False
        elif char.isspace() or char in ",.!?;:，。！？；：、()[]{}\"'":
            latin_run = False
        elif not latin_run:
            count += 1
            latin_run = True
    return count + (chinese_count + 1) // 2


def _stream_chunk_parts(chunk: Any) -> tuple[str, str | None, bool]:
    """从 LangChain chunk 提取可见文本、结束原因和工具调用标记。"""
    content = getattr(chunk, "content", None)
    finish_reason: str | None = None
    metadata = getattr(chunk, "response_metadata", None)
    if isinstance(metadata, Mapping):
        value = (
            metadata.get("finish_reason")
            or metadata.get("stop_reason")
            or metadata.get("done_reason")
        )
        if isinstance(value, str):
            finish_reason = value
    generation_info = getattr(chunk, "generation_info", None)
    if finish_reason is None and isinstance(generation_info, Mapping):
        value = generation_info.get("finish_reason") or generation_info.get("stop_reason")
        if isinstance(value, str):
            finish_reason = value
    additional_kwargs = getattr(chunk, "additional_kwargs", None)
    has_tool_call = bool(
        getattr(chunk, "tool_calls", None)
        or getattr(chunk, "tool_call_chunks", None)
        or (
            additional_kwargs.get("tool_calls")
            if isinstance(additional_kwargs, Mapping)
            else None
        )
    )
    if isinstance(content, str):
        return content, finish_reason, has_tool_call
    if isinstance(content, list):
        visible: list[str] = []
        for item in content:
            if not isinstance(item, Mapping):
                continue
            if item.get("type") == "text" and isinstance(item.get("text"), str):
                visible.append(item["text"])
            elif item.get("type") in {"tool_use", "function_call"}:
                has_tool_call = True
        return "".join(visible), finish_reason, has_tool_call
    return "", finish_reason, has_tool_call


def _normalize_finish_reason(value: str) -> Literal["stop", "length", "filtered", "unknown"]:
    """将常见供应商结束原因映射到回答生成契约。"""
    normalized = value.strip().lower()
    if normalized in {"stop", "end_turn", "completed"}:
        return "stop"
    if normalized in {"length", "max_tokens", "token_limit"}:
        return "length"
    if normalized in {"content_filter", "filtered", "safety"}:
        return "filtered"
    return "unknown"


__all__ = ["ConfiguredLLMProvider"]
