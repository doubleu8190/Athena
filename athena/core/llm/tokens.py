"""面向提供者的 token 计数和响应用量提取。"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

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
    """本地模型 tokenizer 可靠时使用它，否则采用保守估算。"""

    def __init__(self, model: BaseChatModel) -> None:
        """初始化当前对象。

        参数：
            model (BaseChatModel): 输入参数；其类型和取值约束由方法签名及实现定义。

        返回值：
            None: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        self._model = model
    # ChatAnthropic 的计数器会发起同步 API 请求，而通用的 LangChain/Ollama
    # 计数器使用的 tokenizer 与所选模型无关。
        self._use_model_tokenizer = type(model).__module__.startswith(
            "langchain_openai."
        )

    def count_text_tokens(self, text: str) -> int:
        """执行“count text tokens”操作。

        参数：
            text (str): 待处理文本。

        返回值：
            int: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        if not text:
            return 0
        if self._use_model_tokenizer:
            try:
                return len(self._model.get_token_ids(text))
            except (KeyError, NotImplementedError, ValueError):
                pass
        return conservative_text_token_count(text)

    def count_message_tokens(self, messages: Sequence[BaseMessage]) -> int:
        """执行“count message tokens”操作。

        参数：
            messages (Sequence[BaseMessage]): LangChain 消息序列。

        返回值：
            int: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        if not messages:
            return 0
        if self._use_model_tokenizer:
            try:
                return self._model.get_num_tokens_from_messages(list(messages))
            except (KeyError, NotImplementedError, ValueError):
                pass
        return sum(self._count_message_fallback(message) for message in messages) + 3

    def _count_message_fallback(self, message: BaseMessage) -> int:
        """执行“count message fallback”操作。

        参数：
            message (BaseMessage): 输入参数；其类型和取值约束由方法签名及实现定义。

        返回值：
            int: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
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
    """执行“从消息获取 token 用量”操作。

    参数：
        message (BaseMessage): 输入参数；其类型和取值约束由方法签名及实现定义。

    返回值：
        TokenUsage | None: 操作结果；具体语义由调用场景决定。

    异常：
        Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
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
    """执行“build usage”操作。

    参数：
        input_tokens (object): 输入参数；其类型和取值约束由方法签名及实现定义。
        output_tokens (object): 输入参数；其类型和取值约束由方法签名及实现定义。

    返回值：
        TokenUsage | None: 操作结果；具体语义由调用场景决定。

    异常：
        Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
    """
    if not isinstance(input_tokens, int) or not isinstance(output_tokens, int):
        return None
    if input_tokens < 0 or output_tokens < 0:
        return None
    return TokenUsage(input_tokens=input_tokens, output_tokens=output_tokens)
