"""集中注册所有 SQLAlchemy 模型，供迁移和测试建表使用。"""

from adapter.auth.model import AuthSessionModel, UserModel
from adapter.base import Base
from adapter.mcp.model import McpServerModel, McpToolModel
from adapter.message.model import ConversationMessageModel, ExecutionMessageModel
from adapter.project.model import ProjectModel
from adapter.session.model import SessionModel

__all__ = [
    "AuthSessionModel",
    "Base",
    "ConversationMessageModel",
    "ExecutionMessageModel",
    "McpServerModel",
    "McpToolModel",
    "ProjectModel",
    "SessionModel",
    "UserModel",
]
