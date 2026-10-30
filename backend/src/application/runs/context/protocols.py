"""上下文采集能力的应用协议。"""

from __future__ import annotations

from typing import Protocol

from .contracts import ContextPlan, ProviderResult


class ContextProvider(Protocol):
    """按会话和 ContextPlan 检索上下文的应用端口。
    """
    name: str

    async def retrieve_context(
        self,
        *,
        session_id: str,
        run_id: str,
        plan: ContextPlan,
    ) -> ProviderResult:
        """按会话、Run 和已校验计划检索上下文资料。

        参数：
        - `session_id`（str）：目标会话的稳定标识，用于限定数据范围；约束：无额外运行时限制。
        - `run_id`（str）：目标运行的稳定标识，用于关联执行及其记录；约束：无额外运行时限制。
        - `plan`（ContextPlan）：当前计划对象，提供请求的检索或执行安排；约束：无额外运行时限制。

        返回：
        - ProviderResult：检索提供方的状态、候选结果和追踪信息。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        ...


__all__ = ["ContextProvider"]
