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
SCHEMA_VERSION = 5

# 按版本号排列的迁移 SQL，key 为目标版本号（执行后达到的版本）
# v1: 初始版本（旧字段名 metadata / tool_calls / arguments，无 deleted_time）
# v2: 重命名 *_json 列 + 所有表新增 deleted_time + 新增 memories/memory_fts 表
# v3: messages 表新增 run_id 列（按用户请求/run 归组的前端 join 键）
# v4: steps 表新增 parent_run_id 列（子 Agent 步骤显式指向其父 run）
# v5: sessions 表新增 last_summarized_message_id 列（阈值摘要增量指针）
_MIGRATIONS: dict[int, str] = {
    2: """
    -- ===== sessions 表：重命名 metadata → metadata_json，新增 deleted_time =====
    ALTER TABLE sessions RENAME COLUMN metadata TO metadata_json;
    ALTER TABLE sessions ADD COLUMN deleted_time TEXT;

    -- ===== messages 表：重命名 tool_calls / metadata，新增 deleted_time =====
    ALTER TABLE messages RENAME COLUMN tool_calls TO tool_calls_json;
    ALTER TABLE messages RENAME COLUMN metadata TO metadata_json;
    ALTER TABLE messages ADD COLUMN deleted_time TEXT;

    -- ===== steps 表：重命名 metadata，新增 deleted_time =====
    ALTER TABLE steps RENAME COLUMN metadata TO metadata_json;
    ALTER TABLE steps ADD COLUMN deleted_time TEXT;

    -- ===== tool_call 表：重命名 arguments，新增 deleted_time =====
    ALTER TABLE tool_call RENAME COLUMN arguments TO arguments_json;
    ALTER TABLE tool_call ADD COLUMN deleted_time TEXT;

    -- ===== approval_logs 表：重命名 arguments，新增 deleted_time =====
    ALTER TABLE approval_logs RENAME COLUMN arguments TO arguments_json;
    ALTER TABLE approval_logs ADD COLUMN deleted_time TEXT;
    """,
    3: """
    -- ===== messages 表：新增 run_id 列（按用户请求/run 归组） =====
    ALTER TABLE messages ADD COLUMN run_id TEXT;
    """,
    4: """
    -- ===== steps 表：新增 parent_run_id 列（子 Agent 步骤指向父 run） =====
    ALTER TABLE steps ADD COLUMN parent_run_id TEXT;
    """,
    5: """
    -- ===== sessions 表：新增 last_summarized_message_id 列（阈值摘要增量指针） =====
    ALTER TABLE sessions ADD COLUMN last_summarized_message_id TEXT;
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
        if migration_sql is None:
            continue
        try:
            # SQLite 不支持单次 execute 多条语句，需逐条执行
            for stmt in migration_sql.split(";"):
                stmt = stmt.strip()
                if stmt:
                    await conn.execute(text(stmt))
            await conn.execute(text(f"PRAGMA user_version = {target_version};"))
            logger.info(
                "database_migration_applied",
                version=target_version,
            )
        except Exception as e:
            # 迁移失败可能是列已存在（如全新建库后再跑迁移），记录警告但不中断
            logger.warning("database_migration_skipped", version=target_version, error=str(e))
            # 仍然更新 version 以避免反复尝试
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
