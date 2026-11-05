"""消息内容块及其稳定序列化语义。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal


class ToolResultStatus(StrEnum):
    """工具结果的终态。"""

    SUCCESS = "success"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class TextPart:
    """用户可见或模型可见的文本。"""

    text: str
    type: Literal["text"] = field(default="text", init=False)


@dataclass(frozen=True, slots=True)
class FileRefPart:
    """对文件模块中文件的引用，不在消息中复制文件正文。"""

    file_id: str
    filename: str
    mime_type: str | None = None
    size_bytes: int | None = None
    type: Literal["file_ref"] = field(default="file_ref", init=False)


@dataclass(frozen=True, slots=True)
class ToolCallPart:
    """assistant 发起的一次工具调用。"""

    tool_call_id: str
    name: str
    arguments: dict[str, object] = field(default_factory=dict)
    type: Literal["tool_call"] = field(default="tool_call", init=False)


@dataclass(frozen=True, slots=True)
class ToolResultPart:
    """工具调用产生的安全结果或大型结果引用。"""

    tool_call_id: str
    status: ToolResultStatus
    output: str | None = None
    artifact_id: str | None = None
    type: Literal["tool_result"] = field(default="tool_result", init=False)


type MessagePart = TextPart | FileRefPart | ToolCallPart | ToolResultPart


__all__ = [
    "FileRefPart",
    "MessagePart",
    "TextPart",
    "ToolCallPart",
    "ToolResultPart",
    "ToolResultStatus",
]
