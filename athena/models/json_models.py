"""存放数据库 JSON 字段对应的业务模型。

SQLite 中的 JSON 仍以文本保存；读取后统一转换为这里定义的模型。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ExtensibleJsonModel(BaseModel):
    """允许保留适配器或外部协议额外提供字段的模型。"""

    model_config = ConfigDict(
        extra="allow",
        validate_assignment=True,
        populate_by_name=True,
        serialize_by_alias=True,
    )


class JsonObject(ExtensibleJsonModel):
    """用于表示由数据生产方自行定义结构的开放 JSON 对象。"""


class CommandPayload(JsonObject):
    """版本化命令中会用到的内容。"""

    message: str | None = None
    message_id: str | None = None
    system_prompt: str | None = None
    attachment_ids: list[str] = Field(default_factory=list)
    content: str | None = None
    metadata: dict[str, Any] | None = None
    pinned: bool | None = None
    approval_id: str | None = None
    decision: str | None = None


class ToolArguments(JsonObject):
    """一次工具调用的参数，具体字段由所选工具决定。"""


class ToolCall(ExtensibleJsonModel):
    """消息中保存的标准化工具调用信息。"""

    id: str = ""
    name: str = ""
    args: ToolArguments = Field(default_factory=ToolArguments)

    @model_validator(mode="before")
    @classmethod
    def normalize_arguments_key(cls, value: Any) -> Any:
        if isinstance(value, dict):
            value = dict(value)
            if "args" not in value and "arguments" in value:
                value["args"] = value["arguments"]
            value.pop("arguments", None)
        return value


class JsonSchema(ExtensibleJsonModel):
    """本地工具和 MCP 工具共用的 JSON Schema 字段。"""

    type: str | list[str] | None = None
    title: str | None = None
    description: str | None = None
    properties: dict[str, JsonSchema] = Field(default_factory=dict)
    required: list[str] = Field(default_factory=list)
    items: JsonSchema | None = None
    enum: list[Any] | None = None
    default: Any = None
    additional_properties: bool | JsonSchema | None = Field(
        default=None, alias="additionalProperties"
    )


class FileLocator(ExtensibleJsonModel):
    """文件适配器提供的常见源文件位置信息。"""

    path: str | None = None
    page: int | None = None
    paragraph: int | None = None
    sheet: str | None = None
    row: int | str | None = None
    column: int | str | None = None
    start_line: int | None = None
    end_line: int | None = None
    char_start: int | None = None
    char_end: int | None = None
    image: str | None = None


class FileMetadata(ExtensibleJsonModel):
    """文件或文本分块的元数据，包含系统关心的常见字段。"""

    kind: str | None = None
    language: str | None = None
    languages: dict[str, int] = Field(default_factory=dict)
    line_count: int | None = None
    lines: int | None = None
    row_count: int | None = None
    columns: list[str] = Field(default_factory=list)
    pages: int | None = None
    paragraphs: int | None = None
    width: int | None = None
    height: int | None = None
    files: int | None = None
    chunk_count: int | None = None
    symbol_count: int | None = None
    dependency_count: int | None = None


class FileArtifact(ExtensibleJsonModel):
    """文件仓库保存并返回的处理产物。"""

    id: str
    kind: str
    content: str | None = None
    storage_key: str | None = None
    metadata: FileMetadata = Field(default_factory=FileMetadata)
