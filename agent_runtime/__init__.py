"""Runtime 命令消费、图执行与恢复适配器。"""

from .consumer import CommandConsumer
from .streaming import StreamCoalescer
from .transport import SessionEventBus
from .recovery import RecoveryReconciler
from .langgraph_graph import AgentState, build_graph, invoke_graph
from .cancellation import CancellationRegistry
from .command_notifications import CommandNotifier


def __getattr__(name: str):
    """在应用首次请求时延迟加载重量级领域运行时。

    参数:
        name (str): 要读取的模块属性名。
    返回值:
        Any: 请求的延迟加载对象。
    异常:
        AttributeError: 属性名不是受支持的延迟加载项。
        导入异常: 运行时依赖加载失败时传播底层异常。
    """
    if name == "LangGraphRuntime":
        from .graph_runtime import LangGraphRuntime

        return LangGraphRuntime
    raise AttributeError(name)

__all__ = [
    "CommandConsumer",
    "RecoveryReconciler",
    "StreamCoalescer",
    "SessionEventBus",
    "AgentState",
    "build_graph",
    "invoke_graph",
    "CancellationRegistry",
    "CommandNotifier",
    "LangGraphRuntime",
]
