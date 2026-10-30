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
        """保存对象依赖并初始化运行所需状态。

        参数：
        - `repository`（ToolConfigRepository）：负责读取或持久化领域数据的仓储依赖；约束：无额外运行时限制。
        - `execution`（ToolExecutionPort）：当前执行上下文，用于核对任务、代次和租约身份；约束：无额外运行时限制。

        返回：
        - None：操作完成后不提供返回数据。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        self._repository = repository
        self._execution = execution

    async def list_tools(self) -> list[ToolResponse]:
        """列出工具治理配置。

        参数：
        - 无参数。

        返回：
        - list[ToolResponse]：符合条件的有序结果集合；集合成员。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        return [_to_response(value) for value in await self._repository.list_tools()]

    async def get(self, name: str) -> ToolResponse | None:
        """读取一个工具配置。

        参数：
        - `name`（str）：目标名称，用于标识相应配置、工具或对象；约束：无额外运行时限制。

        返回：
        - ToolResponse | None：工具配置、能力和治理策略组成的响应。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        value = await self._repository.get(name)
        return None if value is None else _to_response(value)

    async def update_governance(self, name: str, *, risk_level: str | None = None, require_approval: bool | None = None, enabled: bool | None = None) -> ToolResponse | None:
        """更新风险、审批和启用状态。

        参数：
        - `name`（str）：目标名称，用于标识相应配置、工具或对象；约束：无额外运行时限制。
        - `risk_level`（str | None）：工具操作的风险等级；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`。
        - `require_approval`（bool | None）：是否要求人工审批；None 表示不修改现有配置；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`。
        - `enabled`（bool | None）：功能是否启用；None 表示不修改现有配置；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`。

        返回：
        - ToolResponse | None：工具配置、能力和治理策略组成的响应。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
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
        """执行一次工具调用。

        参数：
        - `request`（ToolExecutionRequest）：当前请求及其关联身份，用于限定操作范围；约束：无额外运行时限制。

        返回：
        - ToolExecutionResult：封装本次操作输出或状态的 `ToolExecutionResult` 结果对象。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        return await self._execution.execute(request)


__all__ = ["ToolService"]


def _to_response(value: ToolConfig) -> ToolResponse:
    """将工具配置实体转换为应用响应。

    参数：
    - `value`（ToolConfig）：待解析、校验或转换的输入值；约束：无额外运行时限制。

    返回：
    - ToolResponse：工具配置、能力和治理策略组成的响应。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
    """
    return ToolResponse(
        name=value.tool_name,
        description=value.description,
        risk_level=value.risk_level,
        execution_mode=value.execution_mode,
        require_approval=value.require_approval,
        enabled=value.enabled,
        parameters=dict(value.parameters.values),
    )
