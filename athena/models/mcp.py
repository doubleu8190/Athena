"""MCP 服务器相关模型."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class MCPServerConfig(BaseModel):
    """单个 MCP 服务端 的注册配置（与用户输入的 mcp服务端s 项一致）.

    属性：
        command: 启动命令（如 npx / python）
        args: 命令参数
        env: 环境变量（含密钥，仅用于启动子进程，列表接口返回掩码）
    """

    command: str
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)


class MCPServer(BaseModel):
    """已持久化的 MCP 服务器记录."""

    name: str
    config: MCPServerConfig
    created_at: datetime
    deleted_time: datetime | None = None
