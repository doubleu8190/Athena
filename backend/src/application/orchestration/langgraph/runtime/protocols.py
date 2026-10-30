"""由生命周期管理的 LangGraph 资源所实现的运行时端口。
"""

from __future__ import annotations

from typing import Any, Protocol

from langgraph.checkpoint.base import BaseCheckpointSaver


class CheckpointerProvider(Protocol):
    """提供已经启动的检查点保存器，同时隐藏其基础设施实现。
    """

    def get_saver(self) -> BaseCheckpointSaver[Any]:
        """返回已启动且可保存图状态的检查点保存器。

        参数：
        - 无参数。

        返回：
        - BaseCheckpointSaver[Any]：已启动并可供工作流使用的 LangGraph 检查点保存器。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        ...


__all__ = ["CheckpointerProvider"]
