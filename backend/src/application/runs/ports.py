"""Run 命令处理使用的应用执行端口。
"""

from __future__ import annotations

from typing import Protocol

from domain.runs import CommandExecutionResult, RunCommand


class RunExecutorPort(Protocol):
    """通过配置的运行时执行或取消 Run 命令。
    """

    async def execute(self, command: RunCommand) -> CommandExecutionResult:
        """执行已提交的 Run 命令。

        参数：
        - `command`（RunCommand）：领域命令，携带幂等身份和目标对象；约束：无额外运行时限制。

        返回：
        - CommandExecutionResult：封装本次操作输出或状态的 `CommandExecutionResult` 结果对象。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        ...

    async def cancel(self, run_id: str | None) -> None:
        """取消指定 Run 的活动执行。

        参数：
        - `run_id`（str | None）：目标运行的稳定标识，用于关联执行及其记录；约束：允许为 None，按未提供值的分支处理。

        返回：
        - None：操作完成后不提供返回数据。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        ...


__all__ = ["RunExecutorPort"]
