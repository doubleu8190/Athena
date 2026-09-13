"""不同 Agent 角色的工具授权策略。"""

from __future__ import annotations

from dataclasses import dataclass

from .contracts import AgentRole

DELEGATION_TOOL_NAMES = frozenset({"spawn_sub_agent", "spawn_parallel_agents"})


@dataclass(frozen=True, slots=True)
class ToolPolicy:
    """根据可信运行角色过滤工具集合。"""

    role: AgentRole
    allowed_tools: frozenset[str]

    @classmethod
    def for_worker(
        cls, available_tools: list[str], requested_tools: list[str] | None = None
    ) -> ToolPolicy:
        """创建禁止递归委派的 Worker 工具策略。

        参数：
            available_tools: Runtime 当前注册且启用的工具名称。
            requested_tools: Planner 为任务申请的工具；为空时使用全部非委派工具。

        返回值：
            ToolPolicy: 已剔除委派能力的 Worker 策略。

        异常：
            ValueError: Planner 申请了 Runtime 不存在的工具。
        """

        available = set(available_tools) - DELEGATION_TOOL_NAMES
        if requested_tools is None:
            allowed = available
        else:
            requested = set(requested_tools)
            unknown = requested - set(available_tools)
            if unknown:
                raise ValueError(
                    f"worker requested unknown tools: {', '.join(sorted(unknown))}"
                )
            allowed = requested & available
        return cls(role=AgentRole.WORKER, allowed_tools=frozenset(allowed))

    def permits(self, tool_name: str) -> bool:
        """判断角色是否允许调用指定工具。

        参数：
            tool_name: Runtime 注册的工具名称。

        返回值：
            bool: 工具位于当前策略白名单时返回 True。

        异常：
            不主动抛出业务异常。
        """

        return tool_name in self.allowed_tools
