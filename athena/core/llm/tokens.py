"""面向提供者的 token 计数和响应用量提取。"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessageChunk, BaseMessage


class TokenCounter(Protocol):
    """供上下文预算使用方调用的 token 计数能力。"""

    def count_text_tokens(self, text: str) -> int:
        """计算单段文本的 token 数量。"""
        ...

    def count_message_tokens(self, messages: Sequence[BaseMessage]) -> int:
        """计算消息序列的 token 数量。"""
        ...


@dataclass(frozen=True)
class TokenUsage:
    """LLM 响应报告的实际 token 用量。"""

    input_tokens: int
    output_tokens: int


class ModelTokenCounter:
    """按模型家族选择 token 计数策略。

    OpenAI 的聊天模型使用 ``tiktoken``。其他聊天模型不能套用 OpenAI
    编码器：如果模型显式覆盖了 LangChain 的 tokenizer 方法就使用该方法，
    否则退回到不依赖模型厂商的保守估算。
    """

    def __init__(self, model: BaseChatModel) -> None:
        """

        参数：
            model (BaseChatModel): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。

        返回值：
            None: 返回该方法声明类型的业务结果，内容由方法职责确定。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
        """
        self._model = model
        self._openai_counter = (
            OpenAITokenCounter(_model_name(model))
            if _is_openai_model(model)
            else None
        )
        self._has_explicit_tokenizer = _has_explicit_tokenizer(model)

    def count_text_tokens(self, text: str) -> int:
        """

        参数：
            text (str): 待处理文本。

        返回值：
            int: 返回该方法声明类型的业务结果，内容由方法职责确定。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
        """
        if not text:
            return 0
        if self._openai_counter is not None:
            return self._openai_counter.count_text_tokens(text)
        if self._has_explicit_tokenizer:
            try:
                return len(self._model.get_token_ids(text))
            except (KeyError, NotImplementedError, TypeError, ValueError):
                pass
        return conservative_text_token_count(text)

    def count_message_tokens(self, messages: Sequence[BaseMessage]) -> int:
        """

        参数：
            messages (Sequence[BaseMessage]): LangChain 消息序列。

        返回值：
            int: 返回该方法声明类型的业务结果，内容由方法职责确定。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
        """
        if not messages:
            return 0
        if self._openai_counter is not None:
            return self._openai_counter.count_message_tokens(messages)
        return sum(self._count_message_fallback(message) for message in messages) + 3

    def _count_message_fallback(self, message: BaseMessage) -> int:
        """

        参数：
            message (BaseMessage): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。

        返回值：
            int: 返回该方法声明类型的业务结果，内容由方法职责确定。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
        """
        payload: dict[str, object] = {
            "role": message.type,
            "content": message.content,
        }
        tool_calls = getattr(message, "tool_calls", None)
        if tool_calls:
            payload["tool_calls"] = tool_calls
        tool_call_id = getattr(message, "tool_call_id", None)
        if tool_call_id:
            payload["tool_call_id"] = tool_call_id
        serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        return conservative_text_token_count(serialized) + 4


class OpenAITokenCounter:
    """Use the model-specific OpenAI ``tiktoken`` encoding.

    ``encoding_for_model`` knows the current OpenAI model families. New or
    deployment-specific names are mapped to the closest public encoding rather
    than silently using a tokenizer from another provider.
    """

    def __init__(self, model_name: str) -> None:
        self.model_name = model_name or "gpt-4o"
        self._encoding: Any | None = None
        try:
            import tiktoken

            self._encoding = tiktoken.encoding_for_model(self.model_name)
        except KeyError:
            # o-series and GPT-4o use the newer vocabulary; older GPT models
            # are compatible with cl100k_base.
            try:
                import tiktoken

                encoding_name = (
                    "o200k_base"
                    if self.model_name.startswith(("gpt-4o", "o1", "o3", "o4"))
                    else "cl100k_base"
                )
                self._encoding = tiktoken.get_encoding(encoding_name)
            except Exception:
                # Tokenizer data is downloaded lazily by tiktoken. Counting
                # must remain available in offline deployments before the
                # encoding cache has been populated.
                self._encoding = None
        except Exception:
            # Tokenizer data is downloaded lazily by tiktoken. Counting must
            # remain available in offline deployments before the cache exists.
            self._encoding = None

    def count_text_tokens(self, text: str) -> int:
        if not text:
            return 0
        if self._encoding is None:
            return conservative_text_token_count(text)
        return len(self._encoding.encode(text))

    def count_message_tokens(self, messages: Sequence[BaseMessage]) -> int:
        if not messages:
            return 0
        if self._encoding is None:
            return sum(
                conservative_text_token_count(_message_text(message))
                for message in messages
            ) + 3
        # OpenAI's chat wire format adds a small, model-family-specific wrapper
        # around every message. These constants are the documented values for
        # current GPT-4o/GPT-4/GPT-3.5 chat models.
        tokens = 3
        for message in messages:
            tokens += 3
            tokens += len(self._encoding.encode(message.type))
            content = message.content
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False, separators=(",", ":"))
            tokens += len(self._encoding.encode(content))
            name = getattr(message, "name", None)
            if name:
                tokens += 1 + len(self._encoding.encode(str(name)))
            for field in ("tool_calls", "tool_call_id"):
                value = getattr(message, field, None)
                if value:
                    serialized = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
                    tokens += len(self._encoding.encode(serialized))
        return tokens


class EmbeddingTokenCounter:
    """Count text with a vector model's tokenizer, never with ``tiktoken``.

    Sentence-transformers and Hugging Face tokenizers expose compatible
    ``encode`` methods, while tests and lightweight adapters often expose a
    plain callable. The adapter keeps those details out of chunking code.
    """

    def __init__(self, tokenizer: Any) -> None:
        self._tokenizer = tokenizer

    def count_text_tokens(self, text: str) -> int:
        if not text:
            return 0
        encode: Callable[..., Any] = getattr(self._tokenizer, "encode", self._tokenizer)
        try:
            encoded = encode(text, add_special_tokens=True, truncation=False)
        except TypeError:
            encoded = encode(text)
        if isinstance(encoded, int):
            return max(0, encoded)
        if isinstance(encoded, dict):
            encoded = encoded.get("input_ids", ())
        if encoded and isinstance(encoded[0], (list, tuple)):
            encoded = encoded[0]
        return len(encoded)


def _is_openai_model(model: object) -> bool:
    model_type = type(model)
    qualified_name = f"{model_type.__module__}.{model_type.__name__}".lower()
    # ``langchain_openai`` also contains OpenAIEmbeddings. Embedding token
    # limits belong to the embedding provider/tokenizer and must not be routed
    # through a chat model's tiktoken strategy.
    return (
        model_type.__module__.startswith("langchain_openai.")
        and "embedding" not in qualified_name
    )


def _model_name(model: object) -> str:
    for attribute in ("model_name", "model"):
        value = getattr(model, attribute, None)
        if isinstance(value, str) and value:
            return value
    return "gpt-4o"


def _has_explicit_tokenizer(model: object) -> bool:
    """Avoid LangChain's default GPT-2 tokenizer for non-OpenAI models."""
    qualified_name = f"{type(model).__module__}.{type(model).__name__}".lower()
    if "embedding" in qualified_name:
        return False
    method = getattr(type(model), "get_token_ids", None)
    return method is not None and method is not BaseChatModel.get_token_ids


def _message_text(message: BaseMessage) -> str:
    content = message.content
    return content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)


def conservative_text_token_count(text: str) -> int:
    """估算文本 token 数，不假设所有文字都按英文方式分词。"""
    if not text:
        return 0
    ascii_text_count = sum(
        1 for char in text if ord(char) < 128 and (char.isalnum() or char.isspace())
    )
    ascii_punctuation_count = sum(
        1 for char in text if ord(char) < 128 and not (char.isalnum() or char.isspace())
    )
    non_ascii_bytes = sum(len(char.encode("utf-8")) for char in text if ord(char) >= 128)
    return max(
        1,
        math.ceil(
            ascii_text_count / 4
            + ascii_punctuation_count
            + non_ascii_bytes / 2
        ),
    )


def token_usage_from_chunks(chunks: Sequence[AIMessageChunk]) -> TokenUsage | None:
    """从完整的 LangChain 分块流中提取规范化用量。"""
    if not chunks:
        return None
    merged = chunks[0]
    for chunk in chunks[1:]:
        merged = merged + chunk
    usage = _token_usage_from_message(merged)
    if usage is not None:
        return usage
    for chunk in reversed(chunks):
        usage = _token_usage_from_message(chunk)
        if usage is not None:
            return usage
    return None


def _token_usage_from_message(message: BaseMessage) -> TokenUsage | None:
    """

    参数：
        message (BaseMessage): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。

    返回值：
        TokenUsage | None: 返回该方法声明类型的业务结果，内容由方法职责确定。

    异常：
        异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
    """
    usage_metadata = getattr(message, "usage_metadata", None)
    if usage_metadata:
        return _build_usage(
            usage_metadata.get("input_tokens"),
            usage_metadata.get("output_tokens"),
        )

    response_metadata = getattr(message, "response_metadata", None) or {}
    for key in ("token_usage", "usage"):
        nested = response_metadata.get(key)
        if isinstance(nested, dict):
            usage = _build_usage(
                nested.get("input_tokens", nested.get("prompt_tokens")),
                nested.get("output_tokens", nested.get("completion_tokens")),
            )
            if usage is not None:
                return usage
    return _build_usage(
        response_metadata.get("prompt_eval_count"),
        response_metadata.get("eval_count"),
    )


def _build_usage(input_tokens: object, output_tokens: object) -> TokenUsage | None:
    """

    参数：
        input_tokens (object): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。
        output_tokens (object): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。

    返回值：
        TokenUsage | None: 返回该方法声明类型的业务结果，内容由方法职责确定。

    异常：
        异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
    """
    if not isinstance(input_tokens, int) or not isinstance(output_tokens, int):
        return None
    if input_tokens < 0 or output_tokens < 0:
        return None
    return TokenUsage(input_tokens=input_tokens, output_tokens=output_tokens)
