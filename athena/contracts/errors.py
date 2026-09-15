"""Gateway、Runtime 与客户端共享的稳定错误契约。"""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ExecutionError(BaseModel):
    """一次运行失败的机器可读描述。

    参数：
        code: 稳定的业务错误码。
        message: 面向用户或日志的错误摘要。
        error_type: 原始异常类型名称或领域错误类型。
        retryable: 当前错误是否允许 Runtime 自动重试。
        phase: 失败发生的执行阶段。
        category: 可选的错误分类。
        stack: 仅在调试模式或内部持久化边界使用的堆栈文本。
    返回值：
        None：创建结构化错误对象。
    异常：
        不主动抛出业务异常；输入校验失败时由 Pydantic 抛出 ValidationError。
    """

    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    error_type: str = Field(min_length=1)
    retryable: bool = False
    phase: str | None = None
    category: str | None = None
    stack: str | None = None

    @classmethod
    def from_exception(
        cls,
        exc: BaseException,
        *,
        code: str,
        retryable: bool,
        phase: str | None = None,
        category: str | None = None,
        stack: str | None = None,
    ) -> "ExecutionError":
        """从异常构造结构化错误，并避免空异常消息丢失类型信息。

        参数：
            exc: 原始异常实例。
            code: 调用方定义的稳定错误码。
            retryable: 当前错误是否允许重试。
            phase: 可选执行阶段。
            category: 可选错误分类。
            stack: 可选堆栈文本。
        返回值：
            ExecutionError：可写入事件、Checkpoint 和命令结果的错误对象。
        异常：
            不主动抛出业务异常。
        """
        message = str(exc).strip() or type(exc).__name__
        return cls(
            code=code,
            message=message,
            error_type=type(exc).__name__,
            retryable=retryable,
            phase=phase,
            category=category,
            stack=stack,
        )

    @classmethod
    def from_value(
        cls,
        value: Any,
        *,
        code: str,
        retryable: bool = False,
        phase: str | None = None,
    ) -> "ExecutionError":
        """将旧版字符串或字典错误归一化为结构化错误。

        参数：
            value: 旧版错误字符串、字典或现有 ExecutionError。
            code: 无稳定错误码时使用的默认错误码。
            retryable: 无显式字段时使用的重试标志。
            phase: 可选执行阶段。
        返回值：
            ExecutionError：归一化后的错误对象。
        异常：
            不主动抛出业务异常。
        """
        if isinstance(value, cls):
            return value
        if isinstance(value, dict):
            try:
                return cls.model_validate(value)
            except Exception:
                value = value.get("message") or value.get("error") or value
        message = str(value).strip() or code
        return cls(
            code=code,
            message=message,
            error_type="RuntimeError",
            retryable=retryable,
            phase=phase,
        )


class ErrorDetail(StrEnum):
    """HTTP 接口返回的稳定错误标识。

    枚举值使用小写下划线格式，便于客户端按错误类型分支处理；资源名称、
    参数原文和底层异常等动态上下文不放入枚举，而由路由按需补充。
    """

    COMMAND_ID_CONFLICT = "command_id_conflict"
    RUN_ID_CONFLICT = "run_id_conflict"
    SESSION_BUSY = "session_busy"

    RUN_NOT_FOUND = "run_not_found"
    COMMAND_NOT_FOUND = "command_not_found"
    SESSION_NOT_FOUND = "session_not_found"
    APPROVAL_NOT_FOUND = "approval_not_found"
    MEMORY_NOT_FOUND = "memory_not_found"
    ATTACHMENT_NOT_FOUND = "attachment_not_found"
    MCP_SERVER_NOT_FOUND = "mcp_server_not_found"
    TOOL_NOT_REGISTERED = "tool_not_registered"

    INVALID_APPROVAL_ACTION = "invalid_approval_action"
    INVALID_CREDENTIALS = "invalid_credentials"
    AUTH_SECRET_NOT_CONFIGURED = "auth_secret_not_configured"
    AUTHENTICATION_REQUIRED = "authentication_required"
    INVALID_ORIGIN = "invalid_origin"
    INVALID_FILENAME = "invalid_filename"
    CONTENT_EMPTY = "content_empty"
    TITLE_EMPTY = "title_empty"
    INVALID_RISK_LEVEL = "invalid_risk_level"

    RUNTIME_NOT_FOUND = "runtime_not_found"
    AGENT_STORE_NOT_FOUND = "agent_store_not_found"
    REALTIME_TRANSPORT_NOT_FOUND = "realtime_transport_not_found"
