"""SQLite SQLAlchemy 异步引擎和会话工厂。

管理 AsyncEngine 生命周期和 AsyncSession 创建。
使用连接池复用数据库连接，避免频繁创建/销毁。
"""

from __future__ import annotations

import os

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from athena.infrastructure.sqlite.models import FILE_CHUNK_FTS_DDL, MEMORY_FTS_DDL, Base
from athena.utils.logging import get_logger

logger = get_logger(__name__)

# 普通 SQLite 读写使用短暂等待；创建 Run 时会临时切换为非阻塞锁竞争。
SQLITE_BUSY_TIMEOUT_MS = 5000

# 全局引擎和会话工厂
_engine = None
_session_factory: async_sessionmaker[AsyncSession] | None = None
_memory_engine = None
_memory_session_factory: async_sessionmaker[AsyncSession] | None = None


async def _migrate_attachment_ownership(conn: AsyncConnection) -> None:
    """把旧附件表升级为会话或知识库二选一的所有权模型。

    参数：
        conn (AsyncConnection): 当前数据库初始化事务中的异步连接。

    返回值：
        None: 新数据库或已升级数据库不会发生结构变更。

    异常：
        SQL 执行失败时传播底层异常，阻止应用在半迁移结构上启动。
    """
    columns = {
        row[1]: row
        for row in (await conn.execute(text("PRAGMA table_info(attachments)"))).all()
    }
    session_column = columns.get("session_id")
    if "knowledge_base_id" in columns and session_column is not None and not session_column[3]:
        return

    # SQLite 不支持直接移除 NOT NULL；重建表以允许知识库文档没有 session_id。
    await conn.execute(text("DROP TABLE IF EXISTS attachments_v2"))
    await conn.execute(
        text(
            """
            CREATE TABLE attachments_v2 (
                id VARCHAR PRIMARY KEY,
                session_id VARCHAR REFERENCES sessions(id),
                knowledge_base_id VARCHAR REFERENCES knowledge_bases(id),
                message_id VARCHAR REFERENCES messages(id),
                filename VARCHAR NOT NULL,
                mime_type VARCHAR NOT NULL,
                size_bytes INTEGER NOT NULL,
                sha256 VARCHAR NOT NULL,
                storage_key VARCHAR NOT NULL,
                adapter_name VARCHAR,
                adapter_version VARCHAR,
                status VARCHAR NOT NULL DEFAULT 'uploaded',
                capabilities_json TEXT NOT NULL DEFAULT '[]',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                error_message TEXT,
                created_at VARCHAR NOT NULL,
                updated_at VARCHAR NOT NULL,
                deleted_time VARCHAR,
                CONSTRAINT ck_attachment_single_owner CHECK (
                    (session_id IS NOT NULL) != (knowledge_base_id IS NOT NULL)
                )
            )
            """
        )
    )
    await conn.execute(
        text(
            """
            INSERT INTO attachments_v2 (
                id, session_id, knowledge_base_id, message_id, filename,
                mime_type, size_bytes, sha256, storage_key, adapter_name,
                adapter_version, status, capabilities_json, metadata_json,
                error_message, created_at, updated_at, deleted_time
            )
            SELECT id, session_id, NULL, message_id, filename,
                   mime_type, size_bytes, sha256, storage_key, adapter_name,
                   adapter_version, status, capabilities_json, metadata_json,
                   error_message, created_at, updated_at, deleted_time
            FROM attachments
            """
        )
    )
    await conn.execute(text("DROP TABLE attachments"))
    await conn.execute(text("ALTER TABLE attachments_v2 RENAME TO attachments"))
    for index_name, columns_sql in {
        "idx_attachments_session": "session_id",
        "idx_attachments_knowledge_base": "knowledge_base_id",
        "idx_attachments_message": "message_id",
        "idx_attachments_hash": "sha256",
        "idx_attachments_status": "status",
    }.items():
        await conn.execute(
            text(
                f"CREATE INDEX IF NOT EXISTS {index_name} "
                f"ON attachments({columns_sql})"
            )
        )


async def init_engine(db_path: str, memory_db_path: str | None = None) -> None:
    """初始化数据库引擎并创建表结构.

    参数：
        db_path: SQLite 数据库文件路径
    """
    global _engine, _session_factory, _memory_engine, _memory_session_factory

    # 确保目录存在
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)

    url = f"sqlite+aiosqlite:///{db_path}"
    _engine = create_async_engine(
        url,
        echo=False,
        pool_size=5,
        max_overflow=10,
        # SQLite 特有配置
        connect_args={
            "check_same_thread": False,
            "timeout": SQLITE_BUSY_TIMEOUT_MS / 1000,
        },
    )
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)

    async with _engine.begin() as conn:
        await conn.execute(text("PRAGMA journal_mode=WAL"))
        await conn.execute(text(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}"))
        # 创建所有表（CREATE TABLE IF NOT EXISTS，不会修改已有表）
        # 统一 Graph 已取代 continuation 和独立文件任务；旧表不再使用。
        await conn.execute(text("DROP TABLE IF EXISTS agent_continuations"))
        await conn.execute(text("DROP TABLE IF EXISTS processing_tasks"))
        await conn.execute(text("DROP TABLE IF EXISTS file_processing_tasks"))
        await conn.execute(text("DROP TABLE IF EXISTS tool_executions"))
        await conn.execute(text("DROP INDEX IF EXISTS uq_agent_runs_active_session"))
        await conn.run_sync(Base.metadata.create_all)
        await _migrate_attachment_ownership(conn)
        # Reconcile databases from the intermediate step-observability schema
        # with older databases before repositories write tool records.
        tool_call_columns = {
            row[1]
            for row in (await conn.execute(text("PRAGMA table_info(tool_call)"))).all()
        }
        if "step_id" not in tool_call_columns:
            await conn.execute(
                text("ALTER TABLE tool_call ADD COLUMN step_id VARCHAR")
            )
        for name, ddl in {
            "run_id": "VARCHAR",
            "attempt_number": "INTEGER NOT NULL DEFAULT 1",
            "approval_id": "VARCHAR",
        }.items():
            if name not in tool_call_columns:
                await conn.execute(
                    text(f"ALTER TABLE tool_call ADD COLUMN {name} {ddl}")
                )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS idx_tool_call_step ON tool_call(step_id)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS idx_tool_call_approval ON tool_call(approval_id)")
        )
        session_columns = {
            row[1]
            for row in (await conn.execute(text("PRAGMA table_info(sessions)"))).all()
        }
        for name in ("superseded_by", "superseded_at", "source_turn_id", "last_observed_at"):
            if name in session_columns:
                await conn.execute(text(f"ALTER TABLE sessions DROP COLUMN {name}"))
        memory_columns = {
            row[1] for row in (await conn.execute(text("PRAGMA table_info(memories)"))).all()
        }
        for name, ddl in {
            "status": "VARCHAR NOT NULL DEFAULT 'active'",
            "superseded_by": "VARCHAR",
            "superseded_at": "VARCHAR",
            "source_turn_id": "VARCHAR",
            "last_observed_at": "VARCHAR",
        }.items():
            if name not in memory_columns:
                await conn.execute(text(f"ALTER TABLE memories ADD COLUMN {name} {ddl}"))
        await conn.execute(text("""CREATE TABLE IF NOT EXISTS memory_relations (
            source_memory_id VARCHAR NOT NULL, target_memory_id VARCHAR NOT NULL,
            relation_type VARCHAR NOT NULL, created_at VARCHAR NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY (source_memory_id, target_memory_id, relation_type))"""))
        await conn.execute(
            text(
                """CREATE TABLE IF NOT EXISTS memory_processing_jobs (
                    job_id VARCHAR PRIMARY KEY,
                    turn_id VARCHAR NOT NULL UNIQUE,
                    session_id VARCHAR NOT NULL,
                    status VARCHAR NOT NULL,
                    attempt INTEGER NOT NULL DEFAULT 0,
                    available_at VARCHAR NOT NULL,
                    payload_json TEXT NOT NULL,
                    result_json TEXT,
                    error_json TEXT,
                    created_at VARCHAR NOT NULL,
                    updated_at VARCHAR NOT NULL
                )"""
            )
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS idx_memory_jobs_queue ON memory_processing_jobs(status, available_at)")
        )
        # 兼容旧版本：附件关系已从中间表收敛到 attachments.message_id。
        legacy_link_table = await conn.execute(
            text(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'table' AND name = 'message_attachments'"
            )
        )
        if legacy_link_table.scalar_one_or_none() is not None:
            await conn.execute(
                text(
                    """
                    UPDATE attachments
                    SET message_id = (
                        SELECT message_id
                        FROM message_attachments
                        WHERE message_attachments.attachment_id = attachments.id
                        LIMIT 1
                    )
                    WHERE attachments.message_id IS NULL
                      AND EXISTS (
                          SELECT 1
                          FROM message_attachments
                          WHERE message_attachments.attachment_id = attachments.id
                      )
                    """
                )
            )
            await conn.execute(text("DROP TABLE message_attachments"))
        await conn.execute(
            text("UPDATE agent_runs SET status = 'failed', error = '旧文件任务已移除' WHERE status = 'waiting_files'")
        )
        await conn.execute(
            text("UPDATE attachments SET status = 'uploaded' WHERE status = 'queued'")
        )
        snapshot_columns = {
            row[1]
            for row in (await conn.execute(text("PRAGMA table_info(stream_snapshots)"))).all()
        }
        if "stream_type" not in snapshot_columns:
            await conn.execute(
                text("ALTER TABLE stream_snapshots ADD COLUMN stream_type VARCHAR NOT NULL DEFAULT 'answer'")
            )
        if "last_chunk_id" not in snapshot_columns:
            await conn.execute(
                text("ALTER TABLE stream_snapshots ADD COLUMN last_chunk_id INTEGER NOT NULL DEFAULT 0")
            )
        await conn.execute(
            text(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_events_stream_chunk
                ON agent_events(session_id, stream_id, chunk_id)
                WHERE stream_id IS NOT NULL AND chunk_id IS NOT NULL
                """
            )
        )
        event_columns = {
            row[1] for row in (await conn.execute(text("PRAGMA table_info(agent_events)"))).all()
        }
        if "transition_id" not in event_columns:
            await conn.execute(
                text("ALTER TABLE agent_events ADD COLUMN transition_id VARCHAR")
            )
        command_columns = {
            row[1]
            for row in (await conn.execute(text("PRAGMA table_info(agent_commands)"))).all()
        }
        if "claimed_at" not in command_columns:
            await conn.execute(
                text("ALTER TABLE agent_commands ADD COLUMN claimed_at VARCHAR")
            )
        await conn.execute(
            text(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_events_transition
                ON agent_events(session_id, transition_id)
                WHERE transition_id IS NOT NULL
                """
            )
        )
        # Root Run 与多个 Worker Run 可以同时活跃，会话互斥由命令事务判断。
        await conn.execute(text("DROP INDEX IF EXISTS uq_agent_runs_active_session"))
        run_columns = {
            row[1]
            for row in (await conn.execute(text("PRAGMA table_info(agent_runs)"))).all()
        }
        for name, ddl in {
            "parent_run_id": "VARCHAR",
            "root_run_id": "VARCHAR",
            "role": "VARCHAR NOT NULL DEFAULT 'root'",
            "plan_id": "VARCHAR",
            "task_id": "VARCHAR",
            "attempt": "INTEGER NOT NULL DEFAULT 1",
            "depth": "INTEGER NOT NULL DEFAULT 0",
        }.items():
            if name not in run_columns:
                await conn.execute(text(f"ALTER TABLE agent_runs ADD COLUMN {name} {ddl}"))
        await conn.execute(
            text("UPDATE agent_runs SET root_run_id = run_id WHERE root_run_id IS NULL")
        )
        plan_columns = {
            row[1]
            for row in (await conn.execute(text("PRAGMA table_info(agent_plans)"))).all()
        }
        if "aggregation_strategy" in plan_columns:
            await conn.execute(text("ALTER TABLE agent_plans DROP COLUMN aggregation_strategy"))
        approval_columns = {
            row[1]
            for row in (await conn.execute(text("PRAGMA table_info(approvals)"))).all()
        }
        for name in ("plan_id", "task_id", "worker_run_id", "expires_at"):
            if name not in approval_columns:
                await conn.execute(
                    text(f"ALTER TABLE approvals ADD COLUMN {name} VARCHAR")
                )
        # 创建 FTS5 虚拟表（ORM 不支持 FTS5，需原生 DDL）
        await conn.execute(text(MEMORY_FTS_DDL))
        await conn.execute(text(FILE_CHUNK_FTS_DDL))

    # 记忆队列使用独立文件，避免后台任务与核心业务写入争抢同一写锁。
    target_memory_path = memory_db_path or db_path
    if target_memory_path != db_path:
        os.makedirs(os.path.dirname(target_memory_path) or ".", exist_ok=True)
        _memory_engine = create_async_engine(
            f"sqlite+aiosqlite:///{target_memory_path}",
            echo=False,
            pool_size=1,
            max_overflow=0,
            connect_args={
                "check_same_thread": False,
                "timeout": SQLITE_BUSY_TIMEOUT_MS / 1000,
            },
        )
        _memory_session_factory = async_sessionmaker(_memory_engine, expire_on_commit=False)
        async with _memory_engine.begin() as memory_conn:
            await memory_conn.execute(text("PRAGMA journal_mode=WAL"))
            await memory_conn.execute(text(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}"))
            await memory_conn.run_sync(lambda sync_conn: Base.metadata.tables["memories"].create(sync_conn, checkfirst=True))
            await memory_conn.execute(text("""CREATE TABLE IF NOT EXISTS memory_relations (
                source_memory_id VARCHAR NOT NULL, target_memory_id VARCHAR NOT NULL,
                relation_type VARCHAR NOT NULL, created_at VARCHAR NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY (source_memory_id, target_memory_id, relation_type))"""))
            await memory_conn.execute(text(MEMORY_FTS_DDL))
            await memory_conn.execute(text("""CREATE TABLE IF NOT EXISTS memory_processing_jobs (
                job_id VARCHAR PRIMARY KEY, turn_id VARCHAR NOT NULL UNIQUE, session_id VARCHAR NOT NULL,
                status VARCHAR NOT NULL, attempt INTEGER NOT NULL DEFAULT 0, available_at VARCHAR NOT NULL,
                payload_json TEXT NOT NULL, result_json TEXT, error_json TEXT, created_at VARCHAR NOT NULL,
                updated_at VARCHAR NOT NULL)"""))
            await memory_conn.execute(text("CREATE INDEX IF NOT EXISTS idx_memory_jobs_queue ON memory_processing_jobs(status, available_at)"))

        # 旧版本可能已经在核心库中留下队列记录，复制成功后再删除源记录即可安全迁移。
        async with get_session() as source:
            rows = (await source.execute(text("SELECT job_id, turn_id, session_id, status, attempt, available_at, payload_json, result_json, error_json, created_at, updated_at FROM memory_processing_jobs"))).mappings().all()
            memory_rows = (await source.execute(text("""SELECT id, session_id, content, metadata_json, pinned, expires_at,
                created_at, last_accessed, access_count, type, category, confidence, source, status,
                superseded_by, superseded_at, source_turn_id, last_observed_at, deleted_time FROM memories"""))).mappings().all()
            relation_rows = (await source.execute(text("SELECT source_memory_id, target_memory_id, relation_type, created_at, metadata_json FROM memory_relations"))).mappings().all()
        if rows:
            async with get_memory_session() as target:
                for row in rows:
                    await target.execute(text("""INSERT OR IGNORE INTO memory_processing_jobs
                        (job_id, turn_id, session_id, status, attempt, available_at, payload_json, result_json, error_json, created_at, updated_at)
                        VALUES (:job_id, :turn_id, :session_id, :status, :attempt, :available_at, :payload_json, :result_json, :error_json, :created_at, :updated_at)"""), dict(row))
                await target.commit()
        async with get_memory_session() as target:
            memory_target_empty = (await target.execute(text("SELECT count(*) FROM memories"))).scalar_one() == 0
        if memory_target_empty and (memory_rows or relation_rows):
            async with get_memory_session() as target:
                for row in memory_rows:
                    await target.execute(text("""INSERT OR IGNORE INTO memories
                        (id, session_id, content, metadata_json, pinned, expires_at, created_at, last_accessed,
                         access_count, type, category, confidence, source, status, superseded_by, superseded_at,
                         source_turn_id, last_observed_at, deleted_time)
                        VALUES (:id, :session_id, :content, :metadata_json, :pinned, :expires_at, :created_at,
                                :last_accessed, :access_count, :type, :category, :confidence, :source, :status,
                                :superseded_by, :superseded_at, :source_turn_id, :last_observed_at, :deleted_time)"""), dict(row))
                    await target.execute(text("INSERT OR IGNORE INTO memory_fts (content, memory_id) VALUES (:content, :id)"), {"content": row["content"], "id": row["id"]})
                for row in relation_rows:
                    await target.execute(text("""INSERT OR IGNORE INTO memory_relations
                        (source_memory_id, target_memory_id, relation_type, created_at, metadata_json)
                        VALUES (:source_memory_id, :target_memory_id, :relation_type, :created_at, :metadata_json)"""), dict(row))
                await target.commit()
        if rows:
            async with get_session() as source:
                await source.execute(text("DELETE FROM memory_processing_jobs"))
                await source.commit()

    logger.info("database_engine_initialized", db_path=db_path, memory_db_path=target_memory_path)


def get_session() -> AsyncSession:
    """获取 AsyncSession 实例.

    注意：AsyncSession 不等于数据库连接！
    - SQLAlchemy 内部维护连接池（pool_size=5, max_overflow=10）
    - AsyncSession 是轻量级的工作单元（Unit of Work），从连接池借出连接
    - session.close() 后连接归还池，而非关闭
    - 因此"每次操作新建 session"不会创建新连接，仅复用池中连接

    返回值：
        AsyncSession 实例，使用后需调用 session.close()

    异常：
        RuntimeError: 如果引擎未初始化
    """
    if _session_factory is None:
        raise RuntimeError("Database engine not initialized. Call init_engine() first.")
    return _session_factory()


def get_memory_session() -> AsyncSession:
    """获取记忆数据库会话；未拆库时回退到核心数据库。"""
    factory = _memory_session_factory or _session_factory
    if factory is None:
        raise RuntimeError("Database engine not initialized. Call init_engine() first.")
    return factory()


async def close_engine() -> None:
    """关闭数据库引擎，释放连接池中所有连接."""
    global _engine, _session_factory, _memory_engine, _memory_session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None
    if _memory_engine is not None:
        await _memory_engine.dispose()
        _memory_engine = None
        _memory_session_factory = None
    logger.info("database_engine_closed")
