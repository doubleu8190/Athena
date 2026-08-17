"""工具执行上下文 — 通过 ContextVar 注入会话 / 运行 / 调用标识。

文件能力工具（如 read_file、search_file）在执行时需要知道当前的
session_id、run_id 和 tool_call_id，但这些参数不应暴露给 LLM（避免
参数伪造），而是由 ``UnifiedToolManager.call_tool()`` 自动注入。

使用 ``ContextVar`` 实现协程安全的上下文传递，支持并发的多会话工具调用。

Typical usage::

    from athena.core.files.context import set_context, get_context, reset_context

    token = set_context(ToolExecutionContext(session_id, run_id, tool_call_id))
    try:
        ctx = get_context()  # 工具内部读取
    finally:
        reset_context(token)
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class ToolExecutionContext:
    """工具执行上下文，标识当前调用所属的会话、运行和工具调用。

    Attributes:
        session_id: 当前会话 ID。
        run_id: 当前运行 ID（一次完整的 Agent 交互）。
        tool_call_id: 当前工具调用的唯一标识，用于审批留痕和结果关联。
    """

    session_id: str
    run_id: str
    tool_call_id: str


_current: ContextVar[ToolExecutionContext | None] = ContextVar("file_tool_context", default=None)


def set_context(context: ToolExecutionContext):
    """设置当前协程的工具执行上下文。

    Args:
        context: 要注入的执行上下文。

    Returns:
        ContextVar token，用于后续调用 ``reset_context()`` 恢复上下文。
    """
    return _current.set(context)


def reset_context(token) -> None:
    """恢复上下文到指定 token 之前的值。

    Args:
        token: ``set_context()`` 返回的 token 对象。
    """
    _current.reset(token)


def get_context() -> ToolExecutionContext:
    """获取当前协程的工具执行上下文。

    Returns:
        当前的 ``ToolExecutionContext`` 实例。

    Raises:
        RuntimeError: 如果在非工具调用上下文中调用（如直接运行测试
            或未通过 ``UnifiedToolManager`` 执行）。
    """
    context = _current.get()
    if context is None:
        raise RuntimeError("文件能力工具必须在 Agent 工具调用上下文中执行")
    return context
