"""声明式工具定义与调用上下文。

提供工具系统的三个核心抽象：

- ``ToolContext`` — 工具调用上下文（会话/运行/调用标识），通过 ContextVar 注入。
- ``ToolSpec`` — 声明式工具规范，定义工具的名称、描述、处理器和治理参数。
- ``ApprovalPort`` — 审批端口协议，定义审批请求的异步接口。
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any, Protocol

from athena.core.tools.base import NativeHandler
from athena.models.tool import RiskLevel


@dataclass(frozen=True)
class ToolContext:
    """工具调用上下文 — 由运行时注入，模型不可伪造。

    通过 ContextVar 实现协程安全的上下文传递，支持并发的多会话工具调用。

    Attributes:
        session_id: 当前会话 ID。
        run_id: 当前运行 ID。
        tool_call_id: 当前工具调用的唯一标识。
    """

    session_id: str
    run_id: str
    tool_call_id: str


_current_context: ContextVar[ToolContext | None] = ContextVar(
    "athena_tool_context", default=None
)


def set_tool_context(context: ToolContext) -> Token[ToolContext | None]:
    """设置当前协程的工具调用上下文。

    Args:
        context: 要注入的上下文。

    Returns:
        ContextVar token，用于后续调用 ``reset_tool_context()`` 恢复上下文。
    """
    return _current_context.set(context)


def reset_tool_context(token: Token[ToolContext | None]) -> None:
    """恢复上下文到指定 token 之前的值。

    Args:
        token: ``set_tool_context()`` 返回的 token 对象。
    """
    _current_context.reset(token)


def get_tool_context() -> ToolContext:
    """获取当前协程的工具调用上下文。

    Returns:
        当前的 ``ToolContext`` 实例。

    Raises:
        RuntimeError: 在非工具调用上下文中调用时抛出。
    """
    context = _current_context.get()
    if context is None:
        raise RuntimeError("Tool context is only available during a tool invocation")
    return context


@dataclass(frozen=True)
class ToolSpec:
    """声明式工具规范 — 定义工具的完整注册信息。

    用于 ``ToolRegistry.install()`` 批量注册工具到运行时管理器。

    Attributes:
        name: 工具名称（唯一标识）。
        description: 工具功能描述（注入给 LLM）。
        handler: 异步处理函数。
        parameters: JSON Schema 参数定义（可选）。
        risk_level: 风险等级。
        require_approval: 是否需要用户审批。
        enabled: 是否默认启用。
        metadata: 附加元数据。
    """

    name: str
    description: str
    handler: NativeHandler
    parameters: dict[str, Any] | None = None
    risk_level: RiskLevel | str = RiskLevel.LOW
    require_approval: bool = False
    enabled: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)


class ApprovalRequest(Protocol):
    """审批请求协议 — 包含一个 Future 用于等待审批结果。"""

    future: Any


class ApprovalPort(Protocol):
    """审批端口协议 — 定义审批请求的异步接口。

    由 ``ApprovalManager`` 实现，``UnifiedToolManager`` 通过此协议
    与审批系统解耦。
    """

    async def request_approval(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        risk_level: str,
        session_id: str,
        run_id: str,
        tool_call_id: str,
    ) -> ApprovalRequest:
        """提交审批请求。

        Args:
            tool_name: 工具名称。
            arguments: 工具调用参数。
            risk_level: 风险等级。
            session_id: 会话 ID。
            run_id: 运行 ID。
            tool_call_id: 工具调用 ID。

        Returns:
            ``ApprovalRequest`` 实例，包含 ``future`` 用于等待结果。
        """
        ...
