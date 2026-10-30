"""工具治理配置查询用例。"""

from __future__ import annotations

from datetime import datetime, timezone

from domain.tools.query_ports import ToolUsageQueryPort
from domain.tools import ToolConfig, ToolConfigRepository
from application.common.responses import ToolResponse


class ToolQueryService:
    """只读工具治理配置，不依赖工具运行时。"""

    def __init__(self, repository: ToolConfigRepository, usage_port: ToolUsageQueryPort | None = None) -> None:
        """保存对象依赖并初始化运行所需状态。

        参数：
        - `repository`（ToolConfigRepository）：负责读取或持久化领域数据的仓储依赖；约束：无额外运行时限制。
        - `usage_port`（ToolUsageQueryPort | None）：读取工具使用统计的应用端口；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`。

        返回：
        - None：操作完成后不提供返回数据。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        self._repository = repository
        self._usage_port = usage_port

    async def list_tools(self) -> list[ToolResponse]:
        """列出工具。

        参数：
        - 无参数。

        返回：
        - list[ToolResponse]：符合条件的有序结果集合；集合成员。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        return [_to_response(value) for value in await self._repository.list_tools()]

    async def get_tool(self, name: str) -> ToolResponse | None:
        """读取工具。

        参数：
        - `name`（str）：目标名称，用于标识相应配置、工具或对象；约束：无额外运行时限制。

        返回：
        - ToolResponse | None：工具配置、能力和治理策略组成的响应。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        value = await self._repository.get(name)
        return None if value is None else _to_response(value)

    async def usage(self) -> tuple[dict[str, str], int]:
        """读取已配置工具的调用统计。

        参数：
        - 无参数。

        返回：
        - tuple[dict[str, str], int]：按工具名称索引的调用统计映射及其总调用次数。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        if self._usage_port is None:
            return {}, 0
        now = datetime.now(timezone.utc)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return (await self._usage_port.last_called_by_tool(), await self._usage_port.count_calls_since(start))


def _to_response(value: ToolConfig, *, last_called_at: str | None = None) -> ToolResponse:
    """将工具治理实体转换为应用响应。

    参数：
    - `value`（ToolConfig）：待解析、校验或转换的输入值；约束：无额外运行时限制。
    - `last_called_at`（str | None）：工具最近一次调用时间的文本值；None 表示从未调用；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`。

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
        last_called_at=last_called_at,
    )
