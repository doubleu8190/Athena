"""Stable runtime tool definitions and bounded Run snapshots."""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_RUNTIME_TOOLS = 200
MAX_TOOL_SNAPSHOT_BYTES = 2 * 1024 * 1024
MAX_RUNTIME_TOOL_SCHEMA_BYTES = 256 * 1024
_MODEL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


class BuiltinToolSource(BaseModel):
    """A platform-owned tool whose meaning is pinned by a version."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["builtin"] = "builtin"
    version: int = Field(ge=1)


class McpToolSource(BaseModel):
    """Safe MCP routing metadata; never includes URL, command, or secrets."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["mcp"] = "mcp"
    server_id: str = Field(min_length=1, max_length=64)
    remote_name: str = Field(min_length=1, max_length=256)
    config_version: int = Field(ge=1)


type RuntimeToolSource = Annotated[
    BuiltinToolSource | McpToolSource,
    Field(discriminator="kind"),
]


class RuntimeToolDefinition(BaseModel):
    """One immutable tool definition available to a Run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_id: str = Field(min_length=1, max_length=64)
    model_name: str = Field(min_length=1, max_length=128)
    description: str = Field(max_length=8192)
    input_schema: dict[str, Any]
    source: RuntimeToolSource
    requires_approval: bool = False

    @field_validator("description")
    @classmethod
    def _clean_description(cls, value: str) -> str:
        return "".join(character for character in value if unicodedata.category(character) != "Cc")

    @field_validator("model_name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        if _MODEL_NAME_PATTERN.fullmatch(value) is None:
            raise ValueError("runtime_tool_name_invalid")
        return value

    @model_validator(mode="after")
    def _validate_schema(self) -> RuntimeToolDefinition:
        if self.input_schema.get("type", "object") != "object":
            raise ValueError("runtime_tool_schema_invalid")
        try:
            size = len(
                json.dumps(
                    self.input_schema,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
        except (TypeError, ValueError, UnicodeError) as error:
            raise ValueError("runtime_tool_schema_invalid") from error
        if size > MAX_RUNTIME_TOOL_SCHEMA_BYTES:
            raise ValueError("runtime_tool_schema_invalid")
        if self.source.kind == "mcp" and not self.tool_id.startswith("tool_"):
            raise ValueError("runtime_tool_source_invalid")
        if (
            self.source.kind == "builtin"
            and not self.tool_id.startswith("builtin_")
            and self.tool_id != "dispatch_subagents"
        ):
            raise ValueError("runtime_tool_source_invalid")
        return self


class RuntimeToolSnapshot(BaseModel):
    """Bounded immutable catalog frozen before the first model request."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = 1
    tools: tuple[RuntimeToolDefinition, ...] = ()

    @model_validator(mode="after")
    def _validate_capacity(self) -> RuntimeToolSnapshot:
        if len(self.tools) > MAX_RUNTIME_TOOLS:
            raise ValueError("tool_snapshot_too_large")
        names = [tool.model_name for tool in self.tools]
        ids = [tool.tool_id for tool in self.tools]
        if len(set(names)) != len(names) or len(set(ids)) != len(ids):
            raise ValueError("tool_snapshot_duplicate")
        encoded = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > MAX_TOOL_SNAPSHOT_BYTES:
            raise ValueError("tool_snapshot_too_large")
        return self

    def by_model_name(self, name: str) -> RuntimeToolDefinition | None:
        """Resolve a model name only through the frozen catalog."""
        return next((tool for tool in self.tools if tool.model_name == name), None)


__all__ = [
    "BuiltinToolSource",
    "MAX_RUNTIME_TOOL_SCHEMA_BYTES",
    "MAX_RUNTIME_TOOLS",
    "MAX_TOOL_SNAPSHOT_BYTES",
    "McpToolSource",
    "RuntimeToolDefinition",
    "RuntimeToolSnapshot",
    "RuntimeToolSource",
]
