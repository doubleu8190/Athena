"""数据库模块 — SQLAlchemy ORM 实现.

模块结构:
- models.py: ORM 模型定义（软删除策略）
- engine.py: AsyncEngine 和会话工厂
- repository.py: Repository 模式封装 CRUD
- database.py: 兼容层，保持旧 API 不变
- schema.py: 原始 DDL（已废弃，保留向后兼容）
"""

from athena.db.database import Database, close_database, get_database
from athena.db.engine import close_engine, get_session, init_engine
from athena.db.models import (
    ApprovalLogModel,
    Base,
    MessageModel,
    SessionModel,
    StepModel,
    ToolCallModel,
)
from athena.db.repository import (
    ApprovalLogRepository,
    MessageRepository,
    SessionRepository,
    StepRepository,
    ToolCallRepository,
)

__all__ = [
    # 兼容层
    "Database",
    "get_database",
    "close_database",
    # 引擎
    "init_engine",
    "close_engine",
    "get_session",
    # 模型
    "Base",
    "SessionModel",
    "MessageModel",
    "StepModel",
    "ToolCallModel",
    "ApprovalLogModel",
    # Repository
    "SessionRepository",
    "MessageRepository",
    "StepRepository",
    "ToolCallRepository",
    "ApprovalLogRepository",
]
