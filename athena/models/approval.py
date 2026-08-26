"""审批相关模型."""

from __future__ import annotations

import asyncio
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class ApprovalDecision(StrEnum):
    """表示 审批Decision 组件，封装相关状态和行为。
    """
    APPROVED = "approved"
    DENIED = "denied"
    TIMEOUT = "timeout"


class ApprovalRequest:
    """审批请求的完整数据结构.

    使用普通类（非 Pydantic）以容纳 asyncio.Future，
    Future 无法被 Pydantic 序列化。
    """

    __slots__ = (
        "id",
        "tool_name",
        "arguments",
        "risk_level",
        "timeout",
        "created_at",
        "session_id",
        "run_id",
        "future",
        "resolved",
        "resolution",
        "tool_call_id",
        "decided_at",
    )

    def __init__(
        self,
        id: str,
        tool_name: str,
        arguments: dict[str, Any],
        risk_level: str,
        timeout: int,
        created_at: datetime,
        session_id: str,
        run_id: str,
        future: asyncio.Future[bool],
        tool_call_id: str,
    ) -> None:
        """初始化当前对象。

        参数：
            id (str): 资源唯一标识。
            tool_name (str): 输入参数；其类型和取值约束由方法签名及实现定义。
            arguments (dict[str, Any]): 输入参数；其类型和取值约束由方法签名及实现定义。
            risk_level (str): 输入参数；其类型和取值约束由方法签名及实现定义。
            timeout (int): 输入参数；其类型和取值约束由方法签名及实现定义。
            created_at (datetime): 输入参数；其类型和取值约束由方法签名及实现定义。
            session_id (str): 会话唯一标识。
            run_id (str): 运行唯一标识。
            future (asyncio.Future[bool]): 输入参数；其类型和取值约束由方法签名及实现定义。
            tool_call_id (str): 输入参数；其类型和取值约束由方法签名及实现定义。

        返回值：
            None: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        self.id = id
        self.tool_name = tool_name
        self.arguments = arguments
        self.risk_level = risk_level
        self.timeout = timeout
        self.created_at = created_at
        self.session_id = session_id
        self.run_id = run_id
        self.tool_call_id = tool_call_id
        self.future = future
        self.resolved: bool = False
        self.resolution: str = "pending"  # approved/denied/timeout（已批准 / 已拒绝 / 超时）
        self.decided_at: datetime | None = None


class ApprovalLog(BaseModel):
    """审批决策记录（持久化）."""

    id: str
    session_id: str
    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    risk_level: str
    decision: ApprovalDecision
    decision_time_ms: float = 0  # 用户响应耗时
    timestamp: datetime
