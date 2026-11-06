"""MCP Server 的严格连接配置。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit


@dataclass(frozen=True, slots=True)
class StreamableHttpConfig:
    """Streamable HTTP 连接配置，仅允许 URL。"""

    transport: Literal["streamable_http"]
    url: str

    def __post_init__(self) -> None:
        """校验 HTTPS URL，避免配置错误中回显敏感值。"""
        try:
            parsed = urlsplit(self.url)
            port = parsed.port
            valid = (
                self.transport == "streamable_http"
                and parsed.scheme.lower() == "https"
                and parsed.hostname is not None
                and parsed.username is None
                and parsed.password is None
                and not parsed.query
                and not parsed.fragment
                and (port is None or 1 <= port <= 65535)
            )
        except (TypeError, ValueError):
            valid = False
        if not valid:
            raise ValueError("mcp_config_invalid")


@dataclass(frozen=True, slots=True)
class StdioConfig:
    """stdio 连接配置，仅允许绝对 command、argv 和绝对 cwd。"""

    transport: Literal["stdio"]
    command: str
    args: tuple[str, ...] = ()
    cwd: str | None = None

    def __post_init__(self) -> None:
        """校验绝对执行路径和无 NUL 的 argv 内容。"""
        if (
            self.transport != "stdio"
            or not self.command.startswith("/")
            or "\x00" in self.command
            or not isinstance(self.args, tuple)
            or (self.cwd is not None and (not self.cwd.startswith("/") or "\x00" in self.cwd))
            or any(not isinstance(argument, str) or "\x00" in argument for argument in self.args)
        ):
            raise ValueError("mcp_config_invalid")


type McpServerConfig = StreamableHttpConfig | StdioConfig


def parse_server_config(value: object) -> McpServerConfig:
    """严格解析有判别字段的配置映射。

    参数：value 是 JSON 解码后的对象。
    返回：与 transport 对应的不可变 HTTP 或 stdio 配置。
    异常：字段组合、类型或值不合法时抛 ValueError，错误不包含配置内容。
    """
    if not isinstance(value, dict) or not isinstance(value.get("transport"), str):
        raise ValueError("mcp_config_invalid")
    transport = value["transport"]
    if transport == "streamable_http":
        if set(value) != {"transport", "url"} or not isinstance(value.get("url"), str):
            raise ValueError("mcp_config_invalid")
        return StreamableHttpConfig(transport=transport, url=value["url"])
    if transport == "stdio":
        if not set(value) <= {"transport", "command", "args", "cwd"}:
            raise ValueError("mcp_config_invalid")
        if not {"transport", "command"} <= set(value):
            raise ValueError("mcp_config_invalid")
        command = value["command"]
        args = value.get("args", [])
        cwd = value.get("cwd")
        if (
            not isinstance(command, str)
            or not isinstance(args, (list, tuple))
            or any(not isinstance(argument, str) for argument in args)
            or (cwd is not None and not isinstance(cwd, str))
        ):
            raise ValueError("mcp_config_invalid")
        return StdioConfig(transport=transport, command=command, args=tuple(args), cwd=cwd)
    raise ValueError("mcp_config_invalid")


__all__ = ["McpServerConfig", "StdioConfig", "StreamableHttpConfig", "parse_server_config"]
