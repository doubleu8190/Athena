"""SQLAlchemy 异步引擎与会话工厂.

管理 AsyncEngine 生命周期和 AsyncSession 创建。
使用连接池复用数据库连接，避免频繁创建/销毁。

内置基于 PRAGMA user_version 的轻量自动迁移机制：
- 每次表结构变更时递增 SCHEMA_VERSION 并追加对应迁移 SQL
- 启动时自动执行未应用的迁移，避免 create_all() 无法 ALTER 已有表的问题
"""

from __future__ import annotations

import os

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from athena.db.models import MEMORY_FTS_DDL, Base
from athena.utils.logging import get_logger

logger = get_logger(__name__)

# 全局引擎和会话工厂
_engine = None
_session_factory: async_sessionmaker[AsyncSession] | None = None

# ---------------------------------------------------------------------------
# 自动迁移机制 — 基于 PRAGMA user_version
# ---------------------------------------------------------------------------

# 当前 schema 版本号（每次表结构变更时递增）
# v1: 基线 schema（metadata_json 平铺后的全新表结构，见 athena/db/schema.sql）
#     删除了 sessions/steps/messages 的 metadata_json 列；messages 新增
#     step_id / tool_call_record_id / tool_name / type 列；memories 新增
#     type / category / confidence / source 列（metadata_json 仅存任意用户字段）。
# v2: 新增 mcp_servers 表 — 持久化用户注册的 MCP Server 配置（command/args/env）。
#     全新建库由 Base.metadata.create_all 自动建表，迁移 SQL 为已有库兜底。
# v3: 新增 tools 表 — 持久化工具治理配置（risk_level/require_approval/enabled）。
#     MCP 工具和 Native 工具统一存储，支持用户在前端管理页面调优。
# 后续表结构变更在此追加增量迁移（key 为目标版本号）。
SCHEMA_VERSION = 3

_MIGRATIONS: dict[int, str] = {
    2: """
CREATE TABLE IF NOT EXISTS mcp_servers (
    name VARCHAR NOT NULL,
    config_json TEXT NOT NULL DEFAULT '{}',
    created_at VARCHAR NOT NULL,
    deleted_time VARCHAR,
    PRIMARY KEY (name)
)
""",
    3: """
CREATE TABLE IF NOT EXISTS tools (
    tool_name VARCHAR NOT NULL,
    execution_mode VARCHAR NOT NULL,
    server_name VARCHAR,
    remote_name VARCHAR,
    description TEXT NOT NULL DEFAULT '',
    parameters_json TEXT NOT NULL DEFAULT '{}',
    risk_level VARCHAR NOT NULL DEFAULT 'medium',
    require_approval INTEGER NOT NULL DEFAULT 1,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at VARCHAR NOT NULL,
    updated_at VARCHAR NOT NULL,
    PRIMARY KEY (tool_name)
)
""",
}


async def _run_migrations(conn) -> None:
    """执行未应用的数据库迁移.

    通过 PRAGMA user_version 追踪已应用的版本，
    逐版本执行 _MIGRATIONS 中的 SQL。
    """
    result = await conn.execute(text("PRAGMA user_version;"))
    current_version = result.scalar() or 0

    if current_version >= SCHEMA_VERSION:
        return

    logger.info("database_migration_start", current_version=current_version, target=SCHEMA_VERSION)

    for target_version in range(current_version + 1, SCHEMA_VERSION + 1):
        migration_sql = _MIGRATIONS.get(target_version)
        try:
            if migration_sql is not None:
                # SQLite 不支持单次 execute 多条语句，需逐条执行
                for stmt in migration_sql.split(";"):
                    stmt = stmt.strip()
                    if stmt:
                        await conn.execute(text(stmt))
                logger.info("database_migration_applied", version=target_version)
        except Exception as e:
            # 迁移失败可能是列已存在（如全新建库后再跑迁移），记录警告但不中断
            logger.warning("database_migration_skipped", version=target_version, error=str(e))
        # 无论是否有该版本的迁移 SQL，都推进版本号（避免空迁移表时版本卡在 0）
        await conn.execute(text(f"PRAGMA user_version = {target_version};"))

    logger.info("database_migration_done", target=SCHEMA_VERSION)


async def init_engine(db_path: str) -> None:
    """初始化数据库引擎并创建表结构.

    Args:
        db_path: SQLite 数据库文件路径
    """
    global _engine, _session_factory

    # 确保目录存在
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)

    url = f"sqlite+aiosqlite:///{db_path}"
    _engine = create_async_engine(
        url,
        echo=False,
        pool_size=5,
        max_overflow=10,
        # SQLite 特有配置
        connect_args={"check_same_thread": False},
    )
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)

    # 创建所有表（CREATE TABLE IF NOT EXISTS，不会修改已有表）
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # 执行自动迁移（对已有表补列/改名）
        await _run_migrations(conn)
        # 创建 FTS5 虚拟表（ORM 不支持 FTS5，需原生 DDL）
        await conn.execute(text(MEMORY_FTS_DDL))

    logger.info("database_engine_initialized", db_path=db_path)


def get_session() -> AsyncSession:
    """获取 AsyncSession 实例.

    注意：AsyncSession 不等于数据库连接！
    - SQLAlchemy 内部维护连接池（pool_size=5, max_overflow=10）
    - AsyncSession 是轻量级的工作单元（Unit of Work），从连接池借出连接
    - session.close() 后连接归还池，而非关闭
    - 因此"每次操作新建 session"不会创建新连接，仅复用池中连接

    Returns:
        AsyncSession 实例，使用后需调用 session.close()

    Raises:
        RuntimeError: 如果引擎未初始化
    """
    if _session_factory is None:
        raise RuntimeError("Database engine not initialized. Call init_engine() first.")
    return _session_factory()


async def close_engine() -> None:
    """关闭数据库引擎，释放连接池中所有连接."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None
        logger.info("database_engine_closed")
