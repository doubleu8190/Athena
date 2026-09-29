"""PostgreSQL 异步引擎和统一数据库结构初始化。"""

from __future__ import annotations

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from athena.infrastructure.postgre.models import Base
from athena.utils.logging import get_logger

logger = get_logger(__name__)
_engine = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


async def initialize_postgres_engine(
    database_url: str,
    *,
    pool_size: int = 10,
    max_overflow: int = 10,
    embedding_dimension: int = 1024,
    embedding_signature: str = "BAAI/bge-m3:v2:1024",
) -> None:
    """连接 PostgreSQL 并创建完整的单库 schema。"""
    if embedding_dimension < 1:
        raise ValueError("embedding_dimension must be positive")
    global _engine, _session_factory
    _engine = create_async_engine(
        database_url,
        echo=False,
        pool_pre_ping=True,
        pool_size=pool_size,
        max_overflow=max_overflow,
    )
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    async with _engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text("DROP INDEX IF EXISTS idx_memories_embedding_hnsw"))
        await conn.execute(text("DROP INDEX IF EXISTS idx_file_chunks_embedding_hnsw"))
        metadata = await conn.execute(
            text("SELECT signature, dimension FROM embedding_metadata WHERE id = 1")
        )
        current = metadata.first()
        if current is None or current.dimension != embedding_dimension or current.signature != embedding_signature:
            # The user-approved model switch invalidates every stored vector, while
            # preserving the source memories and file chunks for re-indexing.
            await conn.execute(text("UPDATE memories SET embedding = NULL"))
            await conn.execute(text("UPDATE file_chunks SET embedding = NULL"))
            await conn.execute(text(
                f"ALTER TABLE memories ALTER COLUMN embedding TYPE vector({embedding_dimension}) USING NULL::vector({embedding_dimension})"
            ))
            await conn.execute(text(
                f"ALTER TABLE file_chunks ALTER COLUMN embedding TYPE vector({embedding_dimension}) USING NULL::vector({embedding_dimension})"
            ))
            await conn.execute(
                text("""
                    INSERT INTO embedding_metadata (id, signature, dimension)
                    VALUES (1, :signature, :dimension)
                    ON CONFLICT (id) DO UPDATE
                    SET signature = EXCLUDED.signature, dimension = EXCLUDED.dimension
                """),
                {"signature": embedding_signature, "dimension": embedding_dimension},
            )
        await conn.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_memories_embedding_hnsw
            ON memories USING hnsw (embedding vector_cosine_ops)
            WHERE embedding IS NOT NULL AND deleted_time IS NULL
        """))
        await conn.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_file_chunks_embedding_hnsw
            ON file_chunks USING hnsw (embedding vector_cosine_ops)
            WHERE embedding IS NOT NULL AND deleted_time IS NULL
        """))
        # File chunks inherit their version from AttachmentModel. Migrate the
        # old per-chunk version/current markers to soft deletion before dropping
        # them; historical rows remain available for audit, while only rows
        # with deleted_time IS NULL participate in current searches.
        file_chunk_columns = await conn.run_sync(
            lambda sync_conn: {
                column["name"]
                for column in inspect(sync_conn).get_columns("file_chunks")
            }
        )
        if "deleted_time" not in file_chunk_columns:
            await conn.execute(
                text("ALTER TABLE file_chunks ADD COLUMN deleted_time VARCHAR")
            )
        if "is_current" in file_chunk_columns:
            await conn.execute(
                text("UPDATE file_chunks SET deleted_time = COALESCE(deleted_time, CURRENT_TIMESTAMP::text) WHERE is_current = 0")
            )
            await conn.execute(text("ALTER TABLE file_chunks DROP COLUMN is_current"))
        # 旧约束依赖 document_version_id，必须先删除约束和索引，再删除字段。
        await conn.execute(
            text("ALTER TABLE file_chunks DROP CONSTRAINT IF EXISTS uq_file_chunk_version_ordinal")
        )
        await conn.execute(text("DROP INDEX IF EXISTS uq_file_chunk_version_ordinal"))
        await conn.execute(text("DROP INDEX IF EXISTS idx_file_chunks_version"))
        await conn.execute(text("DROP INDEX IF EXISTS ix_file_chunks_document_version_id"))
        if "document_version_id" in file_chunk_columns:
            await conn.execute(
                text("ALTER TABLE file_chunks DROP COLUMN document_version_id")
            )
        await conn.execute(text("DROP INDEX IF EXISTS uq_file_chunk_attachment_ordinal_active"))
        await conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_file_chunk_attachment_ordinal_active ON file_chunks (attachment_id, ordinal) WHERE deleted_time IS NULL"))
        # 运行账本中的计划/任务归属可由 agent_tasks -> agent_plans 推导；
        # root_run_id 也可沿 parent_run_id 关系推导。删除旧列，避免历史库继续
        # 暴露两套可能不一致的执行上下文。
        run_columns = await conn.run_sync(
            lambda sync_conn: {
                column["name"]
                for column in inspect(sync_conn).get_columns("agent_runs")
            }
        )
        for column_name in ("root_run_id", "plan_id", "task_id"):
            if column_name in run_columns:
                await conn.execute(
                    text(f"ALTER TABLE agent_runs DROP COLUMN {column_name}")
                )
        task_result_columns = await conn.run_sync(
            lambda sync_conn: {
                column["name"]
                for column in inspect(sync_conn).get_columns("agent_task_results")
            }
        )
        if "plan_id" in task_result_columns:
            await conn.execute(
                text("ALTER TABLE agent_task_results DROP COLUMN plan_id")
            )
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS memory_processing_jobs (
                job_id VARCHAR PRIMARY KEY, turn_id VARCHAR NOT NULL UNIQUE,
                session_id VARCHAR NOT NULL, status VARCHAR NOT NULL,
                attempt INTEGER NOT NULL DEFAULT 0, available_at VARCHAR NOT NULL,
                payload_json TEXT NOT NULL, result_json TEXT, error_json TEXT,
                created_at VARCHAR NOT NULL, updated_at VARCHAR NOT NULL
            )
        """))
        await conn.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_memory_jobs_queue
            ON memory_processing_jobs(status, available_at)
        """))
    logger.info(
        "postgres_database_initialized", database_url=database_url.split("@")[-1]
    )


def get_session() -> AsyncSession:
    if _session_factory is None:
        raise RuntimeError(
            "Database engine not initialized. Call Database.connect() first."
        )
    return _session_factory()


async def close_postgres_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None
    logger.info("postgres_database_closed")
