"""工具目录内部定义与安全投影。"""

from __future__ import annotations

import json
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

MAX_TOOL_DESCRIPTION_LENGTH = 8192
MAX_TOOL_SCHEMA_BYTES = 256 * 1024


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
    """管理员可见的工具目录来源映射，不含连接配置或秘密。"""

    server_id: str
    remote_name: str
    requires_approval: bool = False


__all__ = [
    "AdminToolDefinition",
    "MAX_TOOL_DESCRIPTION_LENGTH",
    "MAX_TOOL_SCHEMA_BYTES",
    "PublicToolDefinition",
    "ToolDefinition",
]
