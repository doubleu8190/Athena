"""本地知识库后台 worker 进程入口。"""

from __future__ import annotations

import asyncio
import logging
import os
import random
from collections.abc import Awaitable, Callable

from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as SqlAlchemyTimeoutError

from app.knowledge.worker import KnowledgeWorker
from bootstrap.knowledge_worker import (
    KnowledgeWorkerBundle,
    build_knowledge_worker,
    build_knowledge_worker_resources,
)

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
    """装配共享 PostgreSQL、MinIO 和模型资源并运行有界消费协程。"""
    # 资源由 bundle 持有；两阶段构造保证启动顺序不变，失败时只清理已创建部分。
    bundle = KnowledgeWorkerBundle()
    failure: BaseException | None = None
    try:
        build_knowledge_worker_resources(bundle)
        originals = bundle.originals
        models = bundle.models
        assert originals is not None and models is not None
        await originals.start()
        await models.embedding.start()
        await models.counter.start()
        concurrency = build_knowledge_worker(bundle)
        worker = bundle.worker
        assert worker is not None
        await _run_consumers(worker, concurrency)
    except BaseException as error:
        failure = error
    finally:
        # 逆序释放模型、存储与数据库；单个清理失败不阻断后续步骤，
        # 只保留首个异常，函数结束前统一抛出。
        models = bundle.models
        if models is not None:
            try:
                models.counter.close()
            except BaseException as error:
                logger.error(
                    "knowledge_worker_counter_cleanup_failed exception_type=%s",
                    type(error).__name__,
                )
                if failure is None:
                    failure = error
        if models is not None:
            try:
                await models.embedding.aclose()
            except BaseException as error:
                logger.error(
                    "knowledge_worker_embedding_cleanup_failed exception_type=%s",
                    type(error).__name__,
                )
                if failure is None:
                    failure = error
        if bundle.originals is not None:
            try:
                await bundle.originals.aclose()
            except BaseException as error:
                logger.error(
                    "knowledge_worker_storage_cleanup_failed exception_type=%s",
                    type(error).__name__,
                )
                if failure is None:
                    failure = error
        if bundle.database is not None:
            try:
                await bundle.database.dispose()
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
