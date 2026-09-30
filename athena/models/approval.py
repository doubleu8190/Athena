"""审批相关模型."""

from __future__ import annotations

from datetime import datetime
from typing import Any

class ApprovalRequest:
    """审批记录展示模型。审批状态和决定以持久化记录为准。"""

    __slots__ = (
        "id",
        "tool_name",
        "arguments",
        "risk_level",
        "created_at",
        "session_id",
        "run_id",
        "tool_call_id",
    )

    def __init__(
        self,
        id: str,
        tool_name: str,
        arguments: dict[str, Any],
        risk_level: str,
        created_at: datetime,
        session_id: str,
        run_id: str,
        tool_call_id: str,
    ) -> None:
        """

        参数：
            id (str): 资源唯一标识。
            tool_name (str): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。
            arguments (dict[str, Any]): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。
            risk_level (str): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。
            created_at (datetime): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。
            session_id (str): 会话唯一标识。
            run_id (str): 运行唯一标识。
            tool_call_id (str): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。

        返回值：
            None: 返回该方法声明类型的业务结果，内容由方法职责确定。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
        """
        self.id = id
        self.tool_name = tool_name
        self.arguments = arguments
        self.risk_level = risk_level
        self.created_at = created_at
        self.session_id = session_id
        self.run_id = run_id
        self.tool_call_id = tool_call_id
