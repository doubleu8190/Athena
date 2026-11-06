"""MCP 目录业务异常；异常文本只使用固定安全代码。"""


class McpConfigurationError(ValueError):
    """MCP 连接配置不合法。"""


class McpServerNameConflictError(ValueError):
    """MCP Server 全局名称已被占用。"""


class McpConfigVersionConflictError(RuntimeError):
    """Server 配置版本已被并发修改。"""


class McpServerNotFoundError(LookupError):
    """Server 不存在或对调用方不可见。"""


class McpAuthorizationChangedError(PermissionError):
    """写事务复核时发现用户、管理员角色或会话状态已变化。"""


class McpSecretError(RuntimeError):
    """秘密封装、密钥配置或解封失败。"""


class McpToolConflictError(ValueError):
    """工具目录违反唯一性约束。"""


class McpCatalogPersistenceError(RuntimeError):
    """目录数据库操作失败；异常文本不包含连接配置或秘密。"""


class McpDiscoveryError(RuntimeError):
    """MCP 发现操作失败，错误文本仅包含固定安全代码。"""


class McpDiscoveryTimeoutError(McpDiscoveryError):
    """MCP 发现超过总 deadline。"""


class McpDiscoveryProtocolError(McpDiscoveryError):
    """MCP 连接或协议交互失败。"""


class McpDiscoveryCapabilityError(McpDiscoveryError):
    """远端未声明 tools capability。"""


class McpDiscoveryResultError(McpDiscoveryError):
    """远端工具定义或分页结果不符合安全约束。"""


class McpInvalidSessionError(PermissionError):
    """请求身份对应的用户或登录会话已失效。"""


class McpAdminRequiredError(PermissionError):
    """当前有效用户不是管理员。"""


__all__ = [
    "McpAdminRequiredError",
    "McpAuthorizationChangedError",
    "McpCatalogPersistenceError",
    "McpConfigVersionConflictError",
    "McpConfigurationError",
    "McpDiscoveryCapabilityError",
    "McpDiscoveryError",
    "McpDiscoveryProtocolError",
    "McpDiscoveryResultError",
    "McpDiscoveryTimeoutError",
    "McpInvalidSessionError",
    "McpSecretError",
    "McpServerNameConflictError",
    "McpServerNotFoundError",
    "McpToolConflictError",
]
