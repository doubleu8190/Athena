"""工具目录读取端口。"""

from __future__ import annotations

from typing import Protocol

from domain.tool.model import AdminToolDefinition, PublicToolDefinition, ToolPage


class ToolCatalogReader(Protocol):
    """以显式安全投影读取工具目录。"""

    async def list_public(self, *, offset: int, limit: int) -> ToolPage[PublicToolDefinition]:
        """读取供普通调用方使用的工具定义。"""

    async def list_admin(self, *, offset: int, limit: int) -> ToolPage[AdminToolDefinition]:
        """读取包含 Server 来源映射的管理员工具定义。"""


__all__ = ["ToolCatalogReader"]
