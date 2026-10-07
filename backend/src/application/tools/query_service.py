"""工具治理配置查询用例。"""

from __future__ import annotations

from datetime import datetime, timezone

from domain.common.query_ports import ToolUsageQueryPort
from domain.tools import ToolConfig, ToolConfigRepository


class ToolQueryService:
    """只读工具治理配置，不依赖工具运行时。"""

    def __init__(self, repository: ToolConfigRepository, usage_port: ToolUsageQueryPort | None = None) -> None:
        self._repository = repository
        self._usage_port = usage_port

    async def list_tools(self) -> list[ToolConfig]:
        return await self._repository.list_all()

    async def get_tool(self, name: str) -> ToolConfig | None:
        return await self._repository.get(name)

    async def usage(self) -> tuple[dict[str, str], int]:
        if self._usage_port is None:
            return {}, 0
        now = datetime.now(timezone.utc)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return (await self._usage_port.last_called_by_tool(), await self._usage_port.count_calls_since(start))
