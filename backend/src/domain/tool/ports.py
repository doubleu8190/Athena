"""工具目录读取端口。"""

from __future__ import annotations

from typing import Protocol

from domain.tool.model import AdminToolDefinition, PublicToolDefinition, ToolDefinition, ToolPage


class ToolCatalogReader(Protocol):
    """以显式安全投影读取工具目录。"""

    async def list_public(self, *, offset: int, limit: int) -> ToolPage[PublicToolDefinition]:
        """读取供普通调用方使用的工具定义。"""

    async def list_admin(self, *, offset: int, limit: int) -> ToolPage[AdminToolDefinition]:
        """读取包含 Server 来源映射的管理员工具定义。"""

    async def list_internal(self, *, offset: int, limit: int) -> ToolPage[ToolDefinition]:
        """读取受信内部调用所需的完整工具定义。"""

    async def list_server_admin(
        self, server_id: str, *, offset: int, limit: int
    ) -> ToolPage[AdminToolDefinition]:
        """读取指定 Server 的管理员工具定义。"""

    async def get_public_tool(self, tool_id: str) -> PublicToolDefinition | None:
        """按 ID 读取普通调用方安全投影。"""

    async def get_admin_tool(self, tool_id: str) -> AdminToolDefinition | None:
        """按 ID 读取管理员安全投影。"""


__all__ = ["ToolCatalogReader"]
