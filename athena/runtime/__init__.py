"""Runtime 命令消费、图执行与恢复适配器。

重量级模块通过 ``__getattr__`` 延迟加载，避免与 ``athena.core.harness`` /
``tools.manager`` 形成循环导入。仅直接导入无外部依赖的轻量模块。
"""

# ── 轻量模块（直接导入，不触发循环） ──

from .stream_coalescer import StreamCoalescer
from .transport import SessionEventBus
from .cancellation import CancellationRegistry
from .command_notifications import CommandNotifier
from .processor_lifecycle import (
    ProcessAction,
    ProcessDecision,
    ProcessOutcome,
    Processor,
    ProcessorBlocked,
    ProcessorLifecycleRunner,
    run_with_lifecycle,
)
from .attachment_processor import AttachmentProcessContext, AttachmentProcessor

# ── 懒加载：避免循环导入（command_consumer → agent_graph → ... → harness → tools.manager） ──

_lazy = {
    "CommandConsumer": ".command_consumer",
    "RecoveryReconciler": ".recovery_reconciler",
    "AgentState": ".agent_graph",
    "build_graph": ".agent_graph",
    "invoke_graph": ".agent_graph",
    "LangGraphRuntime": ".langgraph_runtime",
    "SubAgentManager": ".sub_agent_manager",
    "SubAgentResult": ".sub_agent_manager",
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
    "ProcessorLifecycleRunner",
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
