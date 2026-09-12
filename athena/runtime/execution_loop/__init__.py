"""可检查点化的 LLM ↔ 工具执行循环子图。

将一次 Agent Run 拆成明确的 LangGraph 状态转移：
``llm_call → execute_tool_batch → llm_call``，在每轮之间持久化和恢复。

内部结构：
- ``initialize`` — 初始化扁平执行状态（纯函数）
- ``llm_call``   — 单次 LLM 调用
- ``execute_tools`` — 并行工具批次执行
- ``finish``     — 关闭流、发布终态
- ``graph``      — 编译子图 + 路由 + 主图适配器
- ``_helpers``   — 停止信号提取与 HarnessTurnExecutor 工厂
"""

from .graph import build_agent_loop, route_after_llm, route_after_tools
from .initialize import initialize_execution
from .llm_call import llm_call
from .execute_tools import execute_tool_batch
from .finish import finish_execution

__all__ = [
    "build_agent_loop",
    "route_after_llm",
    "route_after_tools",
    "initialize_execution",
    "llm_call",
    "execute_tool_batch",
    "finish_execution",
]
