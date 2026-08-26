"""Agent 委派工具声明。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from athena.core.tools.spec import ToolSpec, get_tool_context
from athena.models.tool import RiskLevel

SingleHandler = Callable[[str, str, str], Awaitable[str]]
ParallelHandler = Callable[[list[str], str, str, int], Awaitable[str]]


def build_agent_tool_specs(
    single_handler: SingleHandler,
    parallel_handler: ParallelHandler,
    parallel_description: str,
) -> list[ToolSpec]:
    """执行“build agent tool specs”操作。

    参数：
        single_handler (SingleHandler): 输入参数；其类型和取值约束由方法签名及实现定义。
        parallel_handler (ParallelHandler): 输入参数；其类型和取值约束由方法签名及实现定义。
        parallel_description (str): 输入参数；其类型和取值约束由方法签名及实现定义。

    返回值：
        list[ToolSpec]: 操作结果；具体语义由调用场景决定。

    异常：
        Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
    """
    async def spawn_sub_agent(task: str) -> str:
        """执行“spawn sub agent”操作。

        参数：
            task (str): 输入参数；其类型和取值约束由方法签名及实现定义。

        返回值：
            str: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        context = get_tool_context()
        return await single_handler(task, context.session_id, context.run_id)

    async def spawn_parallel_agents(tasks: list[str], max_turns: int = 5) -> str:
        """执行“spawn parallel agents”操作。

        参数：
            tasks (list[str]): 输入参数；其类型和取值约束由方法签名及实现定义。
            max_turns (int): 输入参数；其类型和取值约束由方法签名及实现定义。

        返回值：
            str: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        context = get_tool_context()
        return await parallel_handler(
            tasks, context.session_id, context.run_id, max_turns
        )

    return [
        ToolSpec(
            name="spawn_sub_agent",
            description=(
                "Create a single sub-agent to handle one independent subtask. "
                "Use spawn_parallel_agents for multiple independent subtasks."
            ),
            handler=spawn_sub_agent,
            parameters={
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "The independent subtask to delegate.",
                    }
                },
                "required": ["task"],
            },
            risk_level=RiskLevel.MEDIUM,
        ),
        ToolSpec(
            name="spawn_parallel_agents",
            description=parallel_description,
            handler=spawn_parallel_agents,
            parameters={
                "type": "object",
                "properties": {
                    "tasks": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 2,
                        "maxItems": 6,
                        "description": "Two to six independent subtasks.",
                    },
                    "max_turns": {
                        "type": "integer",
                        "default": 5,
                        "description": "Maximum turns per sub-agent.",
                    },
                },
                "required": ["tasks"],
            },
            risk_level=RiskLevel.MEDIUM,
        ),
    ]
