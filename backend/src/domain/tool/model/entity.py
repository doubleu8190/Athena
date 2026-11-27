"""工具目录内部定义、审批展示元信息校验与安全投影。"""

from __future__ import annotations

import json
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from common.unset import UNSET, UnsetType

MAX_TOOL_DESCRIPTION_LENGTH = 8192
MAX_TOOL_SCHEMA_BYTES = 256 * 1024
MAX_TOOL_DISPLAY_NAME_LENGTH = 120
MAX_APPROVAL_SUMMARY_TEMPLATE_LENGTH = 500
MAX_PARAMETER_LABELS_COUNT = 50
MAX_PARAMETER_LABEL_KEY_LENGTH = 128
MAX_PARAMETER_LABEL_VALUE_LENGTH = 120


def validate_tool_display_name(value: str | None) -> str | None:
    """校验工具中文展示名：显式 None 表示清空，非空字符串去首尾空白后 1–120 字且禁控制字符。"""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("mcp_tool_metadata_invalid")
    if any(unicodedata.category(ch) == "Cc" for ch in value):
        raise ValueError("mcp_tool_metadata_invalid")
    normalized = value.strip()
    if not normalized or len(normalized) > MAX_TOOL_DISPLAY_NAME_LENGTH:
        raise ValueError("mcp_tool_metadata_invalid")
    return normalized


def validate_approval_summary_template(value: str | None) -> str | None:
    """校验审批摘要模板：允许 \\n\\r\\t，禁其余 Cc 控制字符，去首尾空白后 1–500 字。"""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("mcp_tool_metadata_invalid")
    if any(unicodedata.category(ch) == "Cc" and ch not in {"\n", "\r", "\t"} for ch in value):
        raise ValueError("mcp_tool_metadata_invalid")
    normalized = value.strip()
    if not normalized or len(normalized) > MAX_APPROVAL_SUMMARY_TEMPLATE_LENGTH:
        raise ValueError("mcp_tool_metadata_invalid")
    return normalized


def validate_parameter_labels(value: Mapping[str, str] | None) -> dict[str, str]:
    """校验参数中文标签映射：显式 None 清空为 {}，最多 50 项，键值均禁止控制字符。"""
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("mcp_tool_metadata_invalid")
    if len(value) > MAX_PARAMETER_LABELS_COUNT:
        raise ValueError("mcp_tool_metadata_invalid")
    normalized: dict[str, str] = {}
    for raw_key, raw_label in value.items():
        if not isinstance(raw_key, str) or not isinstance(raw_label, str):
            raise ValueError("mcp_tool_metadata_invalid")
        if any(unicodedata.category(ch) == "Cc" for ch in raw_key) or any(
            unicodedata.category(ch) == "Cc" for ch in raw_label
        ):
            raise ValueError("mcp_tool_metadata_invalid")
        key = raw_key.strip()
        label = raw_label.strip()
        if (
            not key
            or len(key) > MAX_PARAMETER_LABEL_KEY_LENGTH
            or not label
            or len(label) > MAX_PARAMETER_LABEL_VALUE_LENGTH
            or key in normalized
        ):
            raise ValueError("mcp_tool_metadata_invalid")
        normalized[key] = label
    return normalized


@dataclass(frozen=True, slots=True)
class ToolMetadataPatch:
    """管理员对单个 MCP 工具审批开关与业务展示元信息的局部更新补丁。"""

    requires_approval: bool | UnsetType = UNSET
    display_name: str | None | UnsetType = UNSET
    approval_summary_template: str | None | UnsetType = UNSET
    parameter_labels: Mapping[str, str] | None | UnsetType = UNSET

    def has_updates(self) -> bool:
        """判断是否至少提供一个更新字段。"""
        return any(
            item is not UNSET
            for item in (
                self.requires_approval,
                self.display_name,
                self.approval_summary_template,
                self.parameter_labels,
            )
        )


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """内部工具记录，含 MCP 来源映射，不含 Server 配置或秘密。"""

    tool_id: str
    server_id: str
    remote_name: str
    model_name: str
    description: str
    input_schema: Mapping[str, Any]
    requires_approval: bool = False

    def __post_init__(self) -> None:
        """校验工具标识、描述和 JSON Schema 对象形状。"""
        clean_description = "".join(
            character for character in self.description if unicodedata.category(character) != "Cc"
        )
        object.__setattr__(self, "description", clean_description)
        valid_tool_id = False
        if self.tool_id.startswith("tool_"):
            raw_uuid = self.tool_id.removeprefix("tool_")
            try:
                parsed_uuid = UUID(hex=raw_uuid)
                valid_tool_id = (
                    parsed_uuid.version == 4
                    and parsed_uuid.hex == raw_uuid
                    and self.model_name == f"mcp__{raw_uuid}"
                )
            except ValueError:
                pass
        if (
            not valid_tool_id
            or not self.server_id
            or not self.remote_name
            or not self.model_name
            or len(self.remote_name) > 256
            or len(clean_description) > MAX_TOOL_DESCRIPTION_LENGTH
            or any(unicodedata.category(character) == "Cc" for character in self.remote_name)
            or not isinstance(self.input_schema, Mapping)
            or self.input_schema.get("type", "object") != "object"
        ):
            raise ValueError("mcp_tool_definition_invalid")
        try:
            schema_size = len(
                json.dumps(
                    dict(self.input_schema),
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
        except (TypeError, ValueError, UnicodeError) as error:
            raise ValueError("mcp_tool_definition_invalid") from error
        if schema_size > MAX_TOOL_SCHEMA_BYTES:
            raise ValueError("mcp_tool_definition_invalid")


@dataclass(frozen=True, slots=True)
class PublicToolDefinition:
    """普通调用方可见的工具描述，不含远端或 Server 来源字段。"""

    tool_id: str
    model_name: str
    description: str
    input_schema: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class AdminToolDefinition(PublicToolDefinition):
    """管理员可见的工具目录来源映射与审批展示元信息，不含连接配置或秘密。"""

    server_id: str
    remote_name: str
    requires_approval: bool = False
    display_name: str | None = None
    approval_summary_template: str | None = None
    parameter_labels: Mapping[str, str] = field(default_factory=dict)


__all__ = [
    "AdminToolDefinition",
    "MAX_APPROVAL_SUMMARY_TEMPLATE_LENGTH",
    "MAX_PARAMETER_LABELS_COUNT",
    "MAX_PARAMETER_LABEL_KEY_LENGTH",
    "MAX_PARAMETER_LABEL_VALUE_LENGTH",
    "MAX_TOOL_DESCRIPTION_LENGTH",
    "MAX_TOOL_DISPLAY_NAME_LENGTH",
    "MAX_TOOL_SCHEMA_BYTES",
    "PublicToolDefinition",
    "ToolDefinition",
    "ToolMetadataPatch",
    "validate_approval_summary_template",
    "validate_parameter_labels",
    "validate_tool_display_name",
]
