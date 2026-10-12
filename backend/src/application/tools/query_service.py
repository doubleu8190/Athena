"""工具治理配置查询用例。"""

from __future__ import annotations

from datetime import datetime, timezone

from domain.tools.query_ports import ToolUsageQueryPort
from domain.tools import ToolConfig, ToolConfigRepository
from application.common.responses import ToolResponse


class ToolQueryService:
    """只读工具治理配置，不依赖工具运行时。"""

    def __init__(self, repository: ToolConfigRepository, usage_port: ToolUsageQueryPort | None = None) -> None:
        self._repository = repository
        self._usage_port = usage_port

    async def list_tools(self) -> list[ToolResponse]:
        return [_to_response(value) for value in await self._repository.list_tools()]

    async def get_tool(self, name: str) -> ToolResponse | None:
        value = await self._repository.get(name)
        return None if value is None else _to_response(value)

    async def usage(self) -> tuple[dict[str, str], int]:
        if self._usage_port is None:
            return {}, 0
        now = datetime.now(timezone.utc)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return (await self._usage_port.last_called_by_tool(), await self._usage_port.count_calls_since(start))


def _to_response(value: ToolConfig, *, last_called_at: str | None = None) -> ToolResponse:
    """将工具治理实体转换为应用响应。"""
    return ToolResponse(
        name=value.tool_name,
        description=value.description,
        risk_level=value.risk_level,
        execution_mode=value.execution_mode,
        require_approval=value.require_approval,
        enabled=value.enabled,
        parameters=dict(value.parameters.values),
        last_called_at=last_called_at,
    )
