"""本地知识库后台 worker 进程入口。"""

from __future__ import annotations

import asyncio
import logging
import os
import random
from collections.abc import Awaitable, Callable

from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as SqlAlchemyTimeoutError

from adapter.knowledge.repository import KnowledgeRepository
from adapter.knowledge.storage import MinioOriginalStore
from app.knowledge.worker import KnowledgeWorker
from infrastructure.database import Database
from infrastructure.embedding import LocalEmbedding
from infrastructure.embedding.profile import embedding_profile_id
from infrastructure.knowledge import JiebaChineseTokenizer
from infrastructure.knowledge.parsers import LocalDocumentParser
from infrastructure.knowledge.token_counter import LocalKnowledgeTokenCounter

logger = logging.getLogger(__name__)


async def _consume(
    worker: KnowledgeWorker,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    consecutive_failures = 0
    while True:
        try:
            claimed = await worker.run_once()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if not _is_recoverable_worker_error(error):
                raise
            consecutive_failures += 1
            ceiling = min(30.0, 0.5 * (2 ** min(consecutive_failures - 1, 6)))
            retry_after = random.uniform(0.0, ceiling)
            logger.warning(
                "knowledge_worker_database_retry error_type=%s retry_after=%.2f",
                type(error).__name__,
                retry_after,
            )
            await sleep(retry_after)
            continue
        consecutive_failures = 0
        if not claimed:
            await sleep(1)


def _is_recoverable_worker_error(error: Exception) -> bool:
    """只重试数据库连接中断、数据库暂不可用和系统级连接错误。"""
    if isinstance(error, (OperationalError, InterfaceError, SqlAlchemyTimeoutError, OSError)):
        return True
    if not isinstance(error, DBAPIError):
        return False
    if error.connection_invalidated:
        return True
    sqlstate = getattr(error.orig, "sqlstate", None)
    return isinstance(sqlstate, str) and (
        sqlstate.startswith("08") or sqlstate in {"57P01", "57P02", "57P03", "53300"}
    )


async def _run_consumers(worker: KnowledgeWorker, concurrency: int) -> None:
    """运行 worker 协程组，任何致命异常时取消并等待其他消费者收尾。"""
    async with asyncio.TaskGroup() as tasks:
        for index in range(concurrency):
            tasks.create_task(_consume(worker), name=f"knowledge-consumer-{index}")


async def run_worker() -> None:
    """组装共享 PostgreSQL、MinIO 和模型资源并运行有界消费协程。"""
    database: Database | None = None
    embedding: LocalEmbedding | None = None
    counter: LocalKnowledgeTokenCounter | None = None
    originals: MinioOriginalStore | None = None
    failure: BaseException | None = None
    try:
        database_url = os.getenv("ATHENA_DATABASE_URL")
        if not database_url:
            raise RuntimeError("knowledge_database_required")
        if os.getenv("ATHENA_KNOWLEDGE_OCR_CACHE_ROOT"):
            os.environ["PADDLE_PDX_CACHE_HOME"] = os.environ["ATHENA_KNOWLEDGE_OCR_CACHE_ROOT"]
        database = Database(database_url)
        originals = MinioOriginalStore.from_environment()
        await originals.start()
        model = os.getenv("ATHENA_KNOWLEDGE_EMBEDDING_MODEL", "BAAI/bge-m3")
        revision = os.getenv(
            "ATHENA_KNOWLEDGE_EMBEDDING_REVISION",
            "5617a9f61b028005a4858fdac845db406aefb181",
        )
        embedding = LocalEmbedding(
            model, revision, os.getenv("ATHENA_KNOWLEDGE_EMBEDDING_DEVICE", "cpu")
        )
        counter = LocalKnowledgeTokenCounter(model, revision)
        await embedding.start()
        await counter.start()
        worker = KnowledgeWorker(
            KnowledgeRepository(database.session_factory),
            LocalDocumentParser(ocr_languages="ch"),
            originals,
            embedding,
            JiebaChineseTokenizer(),
            counter,
            parse_memory_mb=int(os.getenv("ATHENA_KNOWLEDGE_PARSE_MEMORY_MB", "4096")),
            profile_id=embedding_profile_id(model, revision),
            owner_concurrency=int(os.getenv("ATHENA_KNOWLEDGE_OWNER_CONCURRENCY", "2")),
        )
        concurrency = int(os.getenv("ATHENA_KNOWLEDGE_WORKER_CONCURRENCY", "2"))
        if concurrency < 1 or concurrency > 16:
            raise ValueError("knowledge_worker_concurrency_invalid")
        await _run_consumers(worker, concurrency)
    except BaseException as error:
        failure = error
    finally:
        if counter is not None:
            try:
                counter.close()
            except BaseException as error:
                logger.error(
                    "knowledge_worker_counter_cleanup_failed exception_type=%s",
                    type(error).__name__,
                )
                if failure is None:
                    failure = error
        if embedding is not None:
            try:
                await embedding.aclose()
            except BaseException as error:
                logger.error(
                    "knowledge_worker_embedding_cleanup_failed exception_type=%s",
                    type(error).__name__,
                )
                if failure is None:
                    failure = error
        if originals is not None:
            try:
                await originals.aclose()
            except BaseException as error:
                logger.error(
                    "knowledge_worker_storage_cleanup_failed exception_type=%s",
                    type(error).__name__,
                )
                if failure is None:
                    failure = error
        if database is not None:
            try:
                await database.dispose()
            except BaseException as error:
                logger.error(
                    "knowledge_worker_database_cleanup_failed exception_type=%s",
                    type(error).__name__,
                )
                if failure is None:
                    failure = error
    if failure is not None:
        raise failure


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
