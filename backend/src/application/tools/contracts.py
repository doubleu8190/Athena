"""工具用例的输入契约。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class McpServerConfigInput:
    """用于创建或更新 MCP 服务连接的校验输入契约。
    """
    command: str | tuple[str, ...]
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    image_id: str | None = None
    network_policy: str = "none"
    enabled: bool = True


__all__ = ["McpServerConfigInput"]
