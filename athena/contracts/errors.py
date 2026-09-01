"""Gateway 与客户端共享的稳定错误详情。"""

from enum import StrEnum


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
