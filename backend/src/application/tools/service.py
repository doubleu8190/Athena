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


class ToolService:
    """管理工具配置，并把执行委托给基础设施端口。"""

    def __init__(self, repository: ToolConfigRepository, execution: ToolExecutionPort) -> None:
        self._repository = repository
        self._execution = execution

    async def list(self) -> list[ToolConfig]:
        """列出工具治理配置。"""
        return await self._repository.list_all()

    async def get(self, name: str) -> ToolConfig | None:
        """读取一个工具配置。"""
        return await self._repository.get(name)

    async def update_governance(self, name: str, *, risk_level: str | None = None, require_approval: bool | None = None, enabled: bool | None = None) -> ToolConfig | None:
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
        return await self._repository.upsert(updated)

    async def execute(self, request: ToolExecutionRequest) -> ToolExecutionResult:
        """执行一次工具调用。"""
        return await self._execution.execute(request)


__all__ = ["ToolService"]
