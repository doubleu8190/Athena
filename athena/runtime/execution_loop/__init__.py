"""可检查点化的 LLM ↔ 工具执行循环子图。

将一次 Agent Run 拆成明确的 LangGraph 状态转移：
``llm_call → prepare_tool_batch → approval_gate → tool_call → collect_tool_results``，
在每轮之间持久化和恢复。

内部结构：
- ``initialize`` — 初始化恢复/派生执行状态（纯函数）
- ``llm_call``   — 单次 LLM 调用
- ``tool_call`` — 单个工具调用
- ``finish``     — 关闭流、发布终态
- ``graph``      — 编译子图 + 路由 + 主图适配器
- ``_helpers``   — 停止信号提取与 TurnExecutor 工厂
"""

from .graph import build_agent_loop
from .initialize import initialize_execution
from .llm_call import llm_call
from .runner import run_agent_loop
from .prepare_tools import prepare_tool_batch
from .approval_gate import approval_gate
from .tool_call import tool_call
from .collect_tools import collect_tool_results
from .finish import finish_execution
from .results import AgentExecutionResult

__all__ = [
    "build_agent_loop",
    "initialize_execution",
    "llm_call",
    "run_agent_loop",
    "prepare_tool_batch",
    "approval_gate",
    "tool_call",
    "collect_tool_results",
    "finish_execution",
    "AgentExecutionResult",
]
