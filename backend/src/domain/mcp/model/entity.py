"""MCP Server 目录中的安全领域对象。"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType

from domain.access import VisibilityGrant, VisibilityGrantInput, VisibilityScope
from domain.mcp.model.config import McpServerConfig, StdioConfig, StreamableHttpConfig

MAX_MCP_SECRET_BYTES = 16 * 1024
_SECRET_PLACEHOLDER_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_.-]*)\}")


class McpTransport(StrEnum):
    """目录支持的 MCP 传输类型。"""

    STREAMABLE_HTTP = "streamable_http"
    STDIO = "stdio"


class McpSecretPurpose(StrEnum):
    """秘密集合绑定的服务用途。"""

    HTTP_HEADERS = "http_headers"
    STDIO_ENVIRONMENT = "stdio_environment"
    LLM_API_KEY = "llm_api_key"


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
    visibility_scope: VisibilityScope = VisibilityScope.COMPANY
    owner_department_id: str | None = None
    visibility_grants: tuple[VisibilityGrant, ...] = ()
    enabled: bool = True

    @property
    def transport(self) -> McpTransport:
        """返回判别配置中的传输类型。"""
        return McpTransport(self.config.transport)


@dataclass(frozen=True, slots=True)
class McpServerConnectionState:
    """仅供受控应用层运维路径（保密合并/重发现）使用的 Server 投影与已解封凭据。"""

    server: McpServer
    secrets: Mapping[str, str] = field(repr=False)

    def __post_init__(self) -> None:
        """冻结凭据视图，防止共享可变字典或意外泄漏。"""
        object.__setattr__(self, "secrets", MappingProxyType(dict(self.secrets)))


def extract_config_secret_placeholders(config: McpServerConfig) -> frozenset[str]:
    """提取配置字符串中引用的全部 ${SECRET} 占位符名称集合。"""
    texts: list[str] = []
    if isinstance(config, StreamableHttpConfig):
        texts.append(config.url)
    elif isinstance(config, StdioConfig):
        texts.append(config.command)
        texts.extend(config.args)
        if config.cwd is not None:
            texts.append(config.cwd)
    placeholders: set[str] = set()
    for text in texts:
        placeholders.update(_SECRET_PLACEHOLDER_PATTERN.findall(text))
    return frozenset(placeholders)


def validate_mcp_secret_placeholders(config: McpServerConfig, secrets: Mapping[str, str]) -> None:
    """确认合并后的密钥集合满足最终 config 引用的全部 ${SECRET} 占位符。"""
    referenced = extract_config_secret_placeholders(config)
    if not referenced:
        return
    if config.transport == "streamable_http":
        available = {name.lower() for name in secrets}
        if any(name.lower() not in available for name in referenced):
            raise ValueError("mcp_secret_placeholder_missing")
    elif not referenced <= set(secrets):
        raise ValueError("mcp_secret_placeholder_missing")


@dataclass(frozen=True, slots=True)
class McpServerWrite:
    """仅供受控应用/仓储写路径使用的 Server 输入，含未封装秘密。"""

    id: str
    name: str
    config: McpServerConfig
    secrets: Mapping[str, str] = field(repr=False)
    visibility_scope: VisibilityScope = VisibilityScope.COMPANY
    owner_department_id: str | None = None
    visibility_grants: tuple[VisibilityGrantInput, ...] = ()

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
    "McpServerConnectionState",
    "McpServerWrite",
    "McpTransport",
    "extract_config_secret_placeholders",
    "validate_mcp_secret_placeholders",
]
