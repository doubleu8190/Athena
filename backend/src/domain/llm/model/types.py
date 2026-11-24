"""与供应商 SDK 无关的 LLM 调用契约。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

LlmImageDetail = Literal["auto", "low", "high"]
LlmImageMimeType = Literal["image/png", "image/jpeg", "image/webp"]


class LlmRole(StrEnum):
    """LLM 上下文中的消息角色。"""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass(frozen=True, slots=True)
class LlmTextPart:
    """多模态消息中的不可变文本内容部件。"""

    text: str


@dataclass(frozen=True, slots=True)
class LlmImagePart:
    """多模态消息中的不可变图片内容部件；base64 载荷禁止进入 repr，防止日志或异常泄露。"""

    mime_type: LlmImageMimeType
    data_base64: str = field(repr=False)
    detail: LlmImageDetail = "auto"
    width: int | None = None
    height: int | None = None


type LlmContentPart = LlmTextPart | LlmImagePart


@dataclass(frozen=True, slots=True)
class LlmMessage:
    """一个完整上下文消息；向后兼容纯文本 content，并支持有序多模态 content_parts。"""

    role: LlmRole
    content: str = ""
    tool_call_id: str | None = None
    name: str | None = None
    tool_calls: tuple[LlmToolCall, ...] = ()
    content_parts: tuple[LlmContentPart, ...] = ()

    def has_image_parts(self) -> bool:
        """判断当前消息是否携带视觉图片部件。"""
        return any(isinstance(part, LlmImagePart) for part in self.content_parts)


@dataclass(frozen=True, slots=True)
class LlmTool:
    """每次请求绑定的工具快照。"""

    name: str
    description: str
    input_schema: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class LlmRequest:
    """单次模型调用请求。"""

    model_profile: str
    messages: tuple[LlmMessage, ...]
    tools: tuple[LlmTool, ...] = ()
    tool_choice: str = "auto"
    max_output_tokens: int | None = None
    temperature: float | None = None
    timeout_seconds: float | None = None

    def has_image_parts(self) -> bool:
        """判断本次模型请求中是否包含任何视觉图片部件。"""
        return any(message.has_image_parts() for message in self.messages)


@dataclass(frozen=True, slots=True)
class LlmText:
    """响应中的有序文本内容。"""

    text: str


@dataclass(frozen=True, slots=True)
class LlmToolCall:
    """响应中的已验证工具调用。"""

    call_id: str
    name: str
    arguments: Mapping[str, Any]


type LlmOutput = LlmText | LlmToolCall


@dataclass(frozen=True, slots=True)
class LlmUsage:
    """上游 token 用量，缺失字段保留为未知。"""

    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class LlmResponse:
    """单次调用的有序结果。"""

    output: tuple[LlmOutput, ...]
    status: str
    incomplete_reason: str | None
    model: str | None
    response_id: str | None
    request_id: str | None
    usage: LlmUsage | None


@dataclass(frozen=True, slots=True)
class TextDelta:
    """文本内容增量。"""

    output_index: int
    content_index: int
    text: str


@dataclass(frozen=True, slots=True)
class ToolCallStarted:
    """工具调用开始事件。"""

    output_index: int
    call_id: str
    name: str


@dataclass(frozen=True, slots=True)
class ToolArgumentsDelta:
    """工具参数原始 JSON 增量。"""

    output_index: int
    call_id: str
    delta: str


@dataclass(frozen=True, slots=True)
class ToolCallCompleted:
    """完整且通过 Schema 校验的工具调用。"""

    output_index: int
    tool_call: LlmToolCall


@dataclass(frozen=True, slots=True)
class ResponseCompleted:
    """上游正常完成后的完整响应。"""

    response: LlmResponse


type LlmStreamEvent = (
    TextDelta | ToolCallStarted | ToolArgumentsDelta | ToolCallCompleted | ResponseCompleted
)


ToolChoice = Literal["auto", "none", "required"]
