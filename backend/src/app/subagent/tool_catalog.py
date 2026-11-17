"""聚合 Subagent 可选的内置工具与已注册 MCP 工具目录。"""

from __future__ import annotations

from app.tool.runtime import (
    KNOWLEDGE_SEARCH_DEFINITION,
    MEMORY_CORRECT_DEFINITION,
    MEMORY_FORGET_DEFINITION,
    MEMORY_REMEMBER_DEFINITION,
    MEMORY_SEARCH_DEFINITION,
    REQUEST_USER_INPUT_DEFINITION,
    SESSION_FILE_LIST_DEFINITION,
    SESSION_FILE_READ_DEFINITION,
    SKILL_EXECUTE_DEFINITION,
    SKILL_LOAD_DEFINITION,
    SKILL_READ_RESOURCE_DEFINITION,
)
from domain.mcp import McpServerRepository
from domain.subagent import (
    DISPATCH_SUBAGENTS_TOOL_ID,
    AvailableToolGroup,
    AvailableToolItem,
    SubagentToolCatalog,
)
from domain.tool import AdminToolDefinition

_BUILTIN_DEFINITIONS = (
    REQUEST_USER_INPUT_DEFINITION,
    SESSION_FILE_LIST_DEFINITION,
    SESSION_FILE_READ_DEFINITION,
    KNOWLEDGE_SEARCH_DEFINITION,
    SKILL_LOAD_DEFINITION,
    SKILL_READ_RESOURCE_DEFINITION,
    SKILL_EXECUTE_DEFINITION,
    MEMORY_SEARCH_DEFINITION,
    MEMORY_REMEMBER_DEFINITION,
    MEMORY_CORRECT_DEFINITION,
    MEMORY_FORGET_DEFINITION,
)

_MCP_PAGE_BATCH_SIZE = 100


class DefaultSubagentToolCatalog(SubagentToolCatalog):
    """聚合内置可用工具与已注册 MCP 工具，明确排除嵌套调度工具。"""

    def __init__(self, mcp_repository: McpServerRepository | None = None) -> None:
        """保存可选的 MCP 工具仓储端口。"""
        self._mcp_repository = mcp_repository

    async def list_available_tool_groups(self) -> tuple[AvailableToolGroup, ...]:
        """按来源分组返回可选工具目录，严禁包含 dispatch_subagents。"""
        builtin_items = tuple(
            AvailableToolItem(
                tool_id=definition.tool_id,
                name=definition.model_name,
                description=definition.description,
                source_type="builtin",
                requires_approval=definition.requires_approval,
            )
            for definition in _BUILTIN_DEFINITIONS
            if definition.tool_id != DISPATCH_SUBAGENTS_TOOL_ID
            and definition.model_name != DISPATCH_SUBAGENTS_TOOL_ID
        )
        groups: list[AvailableToolGroup] = [
            AvailableToolGroup(
                group_key="builtin",
                label="内置工具",
                items=builtin_items,
            )
        ]

        mcp_tools, server_names = await self._list_all_mcp_tools()
        if mcp_tools:
            mcp_items = tuple(
                AvailableToolItem(
                    tool_id=tool.tool_id,
                    name=tool.model_name,
                    description=tool.description,
                    source_type="mcp",
                    requires_approval=tool.requires_approval,
                    server_id=tool.server_id,
                    server_name=server_names.get(tool.server_id),
                )
                for tool in mcp_tools
                if tool.tool_id != DISPATCH_SUBAGENTS_TOOL_ID
                and tool.model_name != DISPATCH_SUBAGENTS_TOOL_ID
            )
            if mcp_items:
                groups.append(
                    AvailableToolGroup(
                        group_key="mcp",
                        label="MCP 工具",
                        items=mcp_items,
                    )
                )
        return tuple(groups)

    async def list_available_tool_ids(self) -> frozenset[str]:
        """返回所有允许被 Subagent 选用的工具 ID 集合。"""
        groups = await self.list_available_tool_groups()
        return frozenset(item.tool_id for group in groups for item in group.items)

    async def _list_all_mcp_tools(self) -> tuple[list[AdminToolDefinition], dict[str, str]]:
        """分页读取全部已注册 MCP 工具及 Server 名称映射。"""
        if self._mcp_repository is None:
            return [], {}
        server_names: dict[str, str] = {}
        server_offset = 0
        while True:
            server_page = await self._mcp_repository.list_page(
                offset=server_offset, limit=_MCP_PAGE_BATCH_SIZE
            )
            for server in server_page.items:
                server_names[server.id] = server.name
            server_offset += len(server_page.items)
            if server_offset >= server_page.total or not server_page.items:
                break

        collected: list[AdminToolDefinition] = []
        offset = 0
        while True:
            page = await self._mcp_repository.list_admin(offset=offset, limit=_MCP_PAGE_BATCH_SIZE)
            collected.extend(page.items)
            offset += len(page.items)
            if offset >= page.total or not page.items:
                break
        return collected, server_names


__all__ = ["DefaultSubagentToolCatalog"]
