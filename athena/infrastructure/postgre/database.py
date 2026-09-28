"""PostgreSQL 数据库门面。

数据库 持有各 Repository 实例并提供 connect/close 生命周期管理。
调用者通过公开属性直接访问 Repository（如 db.messages.save(msg)）。
所有删除操作为软删除（设置 deleted_time）。
"""

from __future__ import annotations

from athena.infrastructure.postgre.engine import close_postgres_engine, initialize_postgres_engine
from athena.infrastructure.postgre.repositories import (
    FileRepository,
    KnowledgeBaseRepository,
    KnowledgeDocumentJobRepository,
    MCPServerRepository,
    MessageRepository,
    RetrievalTraceRepository,
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

    def __init__(self, database_url: str) -> None:
        """

        参数：
            db_path (str): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。

        返回值：
            None: 返回该方法声明类型的业务结果，内容由方法职责确定。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
        """
        self._database_url = database_url
        self.sessions = SessionRepository()
        self.messages = MessageRepository()
        self.tool_calls = ToolCallRepository()
        self.mcp_servers = MCPServerRepository()
        self.tools = ToolRepository()
        # 编排仓库依赖 runtime.orchestration；延迟导入可避免数据库门面和
        # 编排包在模块加载阶段形成循环依赖。
        from athena.infrastructure.postgre.repositories.orchestration_repository import (
            OrchestrationRepository,
        )

        self.orchestration = OrchestrationRepository()
        self.files = FileRepository()
        self.knowledge_bases = KnowledgeBaseRepository()
        self.knowledge_document_jobs = KnowledgeDocumentJobRepository()
        self.retrieval = RetrievalTraceRepository()

    async def connect(
        self,
        *,
        pool_size: int = 10,
        max_overflow: int = 10,
        embedding_dimension: int = 1024,
        embedding_signature: str = "BAAI/bge-m3:v2:1024",
    ) -> None:
        """建立核心数据库连接，并按需初始化独立的记忆数据库。

        参数:
            memory_db_path: 记忆任务数据库路径；为空时与核心数据库共用连接。
            runtime_db_path: Agent 运行态数据库路径；为空时使用核心库。
            knowledge_db_path: 文件和知识库数据库路径；为空时使用核心库。
            telemetry_db_path: 检索轨迹数据库路径；为空时使用核心库。
        返回:
            None。
        异常:
            数据库初始化失败时传播底层异常。
        """
        await initialize_postgres_engine(
            self._database_url,
            pool_size=pool_size,
            max_overflow=max_overflow,
            embedding_dimension=embedding_dimension,
            embedding_signature=embedding_signature,
        )
        logger.info("database_connected", database="postgresql")

    async def close(self) -> None:
        """关闭数据库连接."""
        await close_postgres_engine()
        logger.info("database_closed")
