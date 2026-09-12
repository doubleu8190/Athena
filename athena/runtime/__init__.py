"""Runtime 命令消费、图执行与恢复适配器。

重量级模块通过 ``__getattr__`` 延迟加载，避免与 ``athena.core.harness`` /
``tools.manager`` 形成循环导入。仅直接导入无外部依赖的轻量模块。
"""

# ── 轻量模块（直接导入，不触发循环） ──

from .streaming import StreamCoalescer
from .transport import SessionEventBus
from .cancellation import CancellationRegistry
from .command_notifications import CommandNotifier
from .processors import (
    ProcessAction,
    ProcessDecision,
    ProcessOutcome,
    Processor,
    ProcessorBlocked,
    StaticProcessorProxy,
    run_with_lifecycle,
)
from .attachment_processor import AttachmentProcessContext, AttachmentProcessor

# ── 懒加载：避免循环导入（consumer → langgraph_graph → ... → harness → tools.manager） ──

_lazy = {
    "CommandConsumer": ".consumer",
    "RecoveryReconciler": ".recovery",
    "AgentState": ".langgraph_graph",
    "build_graph": ".langgraph_graph",
    "invoke_graph": ".langgraph_graph",
    "LangGraphRuntime": ".graph_runtime",
    "SubAgentManager": ".sub_agent",
    "SubAgentResult": ".sub_agent",
}


def __getattr__(name: str):
    module_path = _lazy.get(name)
    if module_path is not None:
        import importlib

        mod = importlib.import_module(module_path, __package__)
        attr = getattr(mod, name)
        globals()[name] = attr
        return attr
    raise AttributeError(name)


__all__ = [
    "StreamCoalescer",
    "SessionEventBus",
    "CancellationRegistry",
    "CommandNotifier",
    "ProcessAction",
    "ProcessDecision",
    "ProcessOutcome",
    "Processor",
    "ProcessorBlocked",
    "StaticProcessorProxy",
    "run_with_lifecycle",
    "AttachmentProcessContext",
    "AttachmentProcessor",
    "CommandConsumer",
    "RecoveryReconciler",
    "AgentState",
    "build_graph",
    "invoke_graph",
    "LangGraphRuntime",
    "SubAgentManager",
    "SubAgentResult",
]
