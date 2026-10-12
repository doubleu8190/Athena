"""工具治理和执行应用服务。"""

from __future__ import annotations

from dataclasses import replace

from domain.tools import (
    RiskLevel,
    ToolConfig,
    ToolConfigRepository,
    ToolExecutionPort,
    ToolExecutionRequest,
    ToolExecutionResult,
)
from application.common.responses import ToolResponse


class ToolService:
    """管理工具配置，并把执行委托给基础设施端口。"""

    def __init__(self, repository: ToolConfigRepository, execution: ToolExecutionPort) -> None:
        self._repository = repository
        self._execution = execution

    async def list_tools(self) -> list[ToolResponse]:
        """列出工具治理配置。"""
        return [_to_response(value) for value in await self._repository.list_tools()]

    async def get(self, name: str) -> ToolResponse | None:
        """读取一个工具配置。"""
        value = await self._repository.get(name)
        return None if value is None else _to_response(value)

    async def update_governance(self, name: str, *, risk_level: str | None = None, require_approval: bool | None = None, enabled: bool | None = None) -> ToolResponse | None:
        """更新风险、审批和启用状态。"""
        current = await self._repository.get(name)
        if current is None:
            return None
        updated = replace(
            current,
            risk_level=RiskLevel(risk_level) if risk_level is not None else current.risk_level,
            require_approval=require_approval if require_approval is not None else current.require_approval,
            enabled=enabled if enabled is not None else current.enabled,
        )
        return _to_response(await self._repository.upsert(updated))

    async def execute(self, request: ToolExecutionRequest) -> ToolExecutionResult:
        """执行一次工具调用。"""
        return await self._execution.execute(request)


__all__ = ["ToolService"]


def _to_response(value: ToolConfig) -> ToolResponse:
    """将工具配置实体转换为应用响应。"""
    return ToolResponse(
        name=value.tool_name,
        description=value.description,
        risk_level=value.risk_level,
        execution_mode=value.execution_mode,
        require_approval=value.require_approval,
        enabled=value.enabled,
        parameters=dict(value.parameters.values),
    )
