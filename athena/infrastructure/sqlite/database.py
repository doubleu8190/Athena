"""SQLite 数据库门面。

数据库 持有各 Repository 实例并提供 connect/close 生命周期管理。
调用者通过公开属性直接访问 Repository（如 db.messages.save(msg)）。
所有删除操作为软删除（设置 deleted_time）。
"""

from __future__ import annotations

from athena.infrastructure.sqlite.engine import close_engine, init_engine
from athena.infrastructure.sqlite.repositories import (
    ApprovalLogRepository,
    McpServerRepository,
    MessageRepository,
    SessionRepository,
    ToolCallRepository,
    ToolRepository,
)
from athena.utils.logging import get_logger

logger = get_logger(__name__)


class Database:
    """异步数据库访问层.

    公开属性暴露各 Repository，调用者直接使用（如 db.messages.save(msg)）。
    connect/close 管理 SQLAlchemy 引擎生命周期。
    """

    def __init__(self, db_path: str) -> None:
        """

        参数：
            db_path (str): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。

        返回值：
            None: 返回该方法声明类型的业务结果，内容由方法职责确定。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
        """
        self._db_path = db_path
        self.sessions = SessionRepository()
        self.messages = MessageRepository()
        self.tool_calls = ToolCallRepository()
        self.approval_logs = ApprovalLogRepository()
        self.mcp_servers = McpServerRepository()
        self.tools = ToolRepository()
        from athena.infrastructure.sqlite.orchestration_repository import (
            OrchestrationRepository,
        )

        self.orchestration = OrchestrationRepository()
        from athena.infrastructure.sqlite.file_repository import FileRepository

        self.files = FileRepository()

    async def connect(self, memory_db_path: str | None = None) -> None:
        """建立核心数据库连接，并按需初始化独立的记忆数据库。

        参数:
            memory_db_path: 记忆任务数据库路径；为空时与核心数据库共用连接。
        返回:
            None。
        异常:
            数据库初始化失败时传播底层异常。
        """
        await init_engine(self._db_path, memory_db_path=memory_db_path)
        logger.info("database_connected", db_path=self._db_path)

    async def close(self) -> None:
        """关闭数据库连接."""
        await close_engine()
        logger.info("database_closed")
