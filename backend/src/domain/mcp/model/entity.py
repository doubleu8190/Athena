"""MCP Server 目录中的安全领域对象。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType

from domain.mcp.model.config import McpServerConfig

MAX_MCP_SECRET_BYTES = 16 * 1024


class McpTransport(StrEnum):
    """目录支持的 MCP 传输类型。"""

    STREAMABLE_HTTP = "streamable_http"
    STDIO = "stdio"


class McpSecretPurpose(StrEnum):
    """秘密集合绑定的服务用途。"""

    HTTP_HEADERS = "http_headers"
    STDIO_ENVIRONMENT = "stdio_environment"


@dataclass(frozen=True, slots=True)
class McpConnectionInput:
    """一次发现操作使用的连接配置与已解封秘密，不含数据库身份。"""

    config: McpServerConfig
    secrets: Mapping[str, str] = field(repr=False)

    def __post_init__(self) -> None:
        """复制并冻结秘密，避免后续操作共享可变凭据。"""
        object.__setattr__(self, "secrets", MappingProxyType(dict(self.secrets)))


@dataclass(frozen=True, slots=True)
class DiscoveredMcpTool:
    """远端 MCP 返回的工具定义，尚未分配本地身份。"""

    remote_name: str
    description: str
    input_schema: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class McpSecretMetadata:
    """可安全展示的秘密键名及是否保存了值。"""

    name: str
    has_value: bool


@dataclass(frozen=True, slots=True)
class McpServer:
    """已保存的 Server 投影，不包含任何秘密明文。"""

    id: str
    name: str
    config: McpServerConfig
    secret_metadata: tuple[McpSecretMetadata, ...]
    key_version: int | None
    config_version: int
    created_by: str | None
    created_at: datetime
    updated_at: datetime
    tools_discovered_at: datetime | None

    @property
    def transport(self) -> McpTransport:
        """返回判别配置中的传输类型。"""
        return McpTransport(self.config.transport)


@dataclass(frozen=True, slots=True)
class McpServerRuntimeConfig:
    """仅供 transport 组装使用的敏感连接配置，不可作为用户响应。"""

    server: McpServer
    secrets: Mapping[str, str] = field(repr=False)

    def __post_init__(self) -> None:
        """复制并冻结运行时秘密映射，避免调用方意外改写读取结果。"""
        object.__setattr__(self, "secrets", MappingProxyType(dict(self.secrets)))


@dataclass(frozen=True, slots=True)
class McpServerWrite:
    """仅供受控应用/仓储写路径使用的 Server 输入，含未封装秘密。"""

    id: str
    name: str
    config: McpServerConfig
    secrets: Mapping[str, str] = field(repr=False)

    def __post_init__(self) -> None:
        """校验展示名、秘密名和秘密总大小，不把输入值写入异常。"""
        if not isinstance(self.secrets, Mapping):
            raise ValueError("mcp_secret_invalid")
        normalized_name = self.name.strip()
        if (
            not normalized_name
            or len(normalized_name) > 120
            or any(ord(character) < 32 for character in normalized_name)
        ):
            raise ValueError("mcp_server_name_invalid")
        if any(
            not isinstance(name, str)
            or not isinstance(secret, str)
            or "\x00" in name
            or "\x00" in secret
            for name, secret in self.secrets.items()
        ):
            raise ValueError("mcp_secret_invalid")
        if (
            self.config.transport == "streamable_http"
            and self.secrets
            and (
                not all(_valid_http_secret_name(name) for name in self.secrets)
                or len({name.lower() for name in self.secrets}) != len(self.secrets)
            )
        ):
            raise ValueError("mcp_secret_name_invalid")
        if self.config.transport == "streamable_http" and any(
            "\r" in secret or "\n" in secret for secret in self.secrets.values()
        ):
            raise ValueError("mcp_secret_invalid")
        if (
            self.config.transport == "stdio"
            and self.secrets
            and not all(_valid_environment_name(name) for name in self.secrets)
        ):
            raise ValueError("mcp_secret_name_invalid")
        try:
            total_bytes = sum(
                len(name.encode("utf-8")) + len(secret.encode("utf-8"))
                for name, secret in self.secrets.items()
            )
        except (AttributeError, UnicodeError) as error:
            raise ValueError("mcp_secret_invalid") from error
        if total_bytes > MAX_MCP_SECRET_BYTES:
            raise ValueError("mcp_secret_invalid")
        object.__setattr__(self, "secrets", MappingProxyType(dict(self.secrets)))

    @property
    def secret_purpose(self) -> McpSecretPurpose:
        """根据 transport 返回唯一秘密用途。"""
        if self.config.transport == "streamable_http":
            return McpSecretPurpose.HTTP_HEADERS
        return McpSecretPurpose.STDIO_ENVIRONMENT


def _valid_http_secret_name(name: str) -> bool:
    """限制 HTTP 凭据名为合法且不覆盖 MCP/HTTP 协议控制字段的 token。"""
    normalized_name = name.lower()
    reserved_names = {
        "host",
        "content-length",
        "connection",
        "transfer-encoding",
        "accept",
        "content-type",
        "mcp-protocol-version",
        "mcp-method",
        "mcp-name",
        "mcp-session-id",
        "last-event-id",
    }
    return (
        bool(name)
        and all(
            (character.isascii() and character.isalnum()) or character in "!#$%&'*+-.^_`|~"
            for character in name
        )
        and normalized_name not in reserved_names
        and not normalized_name.startswith("mcp-param-")
    )


def _valid_environment_name(name: str) -> bool:
    """限制环境变量名为常见 POSIX 标识符。"""
    return (
        bool(name)
        and (name[0].isalpha() or name[0] == "_")
        and all(
            character.isascii() and (character.isalnum() or character == "_") for character in name
        )
    )


__all__ = [
    "MAX_MCP_SECRET_BYTES",
    "McpSecretMetadata",
    "McpSecretPurpose",
    "McpServer",
    "McpServerRuntimeConfig",
    "McpServerWrite",
    "McpTransport",
]
