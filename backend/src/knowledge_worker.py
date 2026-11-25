"""本地知识库后台 worker 进程入口。"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import signal
from collections.abc import Awaitable, Callable
from contextlib import suppress

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


def _log_level() -> str:
    """统一日志级别解析：优先 ATHENA_LOG_LEVEL，其次兼容 LOG_LEVEL，默认 INFO。"""
    return (
        (os.getenv("ATHENA_LOG_LEVEL") or "").strip()
        or (os.getenv("LOG_LEVEL") or "").strip()
        or "INFO"
    ).upper()


async def run_worker(*, stop_event: asyncio.Event | None = None) -> None:
    """装配共享 PostgreSQL、MinIO 和模型资源并运行有界消费协程；支持停机信号优雅退出。"""
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
        await models.start_tokenizer()
        concurrency = build_knowledge_worker(bundle)
        worker = bundle.worker
        assert worker is not None
        if stop_event is None:
            await _run_consumers(worker, concurrency)
        elif not stop_event.is_set():
            # 启动加载模型期间若已收到停机信号，跳过消费循环直接进入 finally 逆序清理。
            consumers_task = asyncio.create_task(_run_consumers(worker, concurrency))
            stop_task = asyncio.create_task(stop_event.wait())
            try:
                done, _ = await asyncio.wait(
                    (consumers_task, stop_task),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if consumers_task in done:
                    await consumers_task
                else:
                    consumers_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await consumers_task
            finally:
                if not stop_task.done():
                    stop_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await stop_task
                if not consumers_task.done():
                    consumers_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await consumers_task
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


async def _main() -> None:
    """安装 SIGINT/SIGTERM 信号处理并运行 Knowledge Worker 生命周期。"""
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed_signals: list[signal.Signals] = []
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signal_name, stop_event.set)
        except NotImplementedError:
            continue
        installed_signals.append(signal_name)
    try:
        await run_worker(stop_event=stop_event)
    finally:
        # 退出前解绑信号处理器，避免复用事件循环时回调残留。
        for signal_name in installed_signals:
            with suppress(NotImplementedError):
                loop.remove_signal_handler(signal_name)


def main() -> None:
    """配置日志并以正常信号语义运行异步入口。"""
    logging.basicConfig(level=_log_level())
    asyncio.run(_main())


if __name__ == "__main__":
    main()
