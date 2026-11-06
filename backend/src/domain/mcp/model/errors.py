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


__all__ = [
    "McpAuthorizationChangedError",
    "McpCatalogPersistenceError",
    "McpConfigVersionConflictError",
    "McpConfigurationError",
    "McpSecretError",
    "McpServerNameConflictError",
    "McpServerNotFoundError",
    "McpToolConflictError",
]
