"""独立记忆生命周期 Worker 进程入口。"""

from __future__ import annotations

import asyncio
import logging
import os
import random
from collections.abc import Awaitable, Callable
from datetime import timedelta

from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as SqlAlchemyTimeoutError

from app.memory.worker import MemoryWorker
from bootstrap.memory_worker import (
    MemoryWorkerBundle,
    build_memory_worker_dependencies,
)
from common.date import utc_now

logger = logging.getLogger(__name__)
DEFAULT_MEMORY_SCAN_LOOKBACK_HOURS = 24
MAX_MEMORY_SCAN_LOOKBACK_HOURS = 24 * 30


def _scan_lookback_hours() -> int:
    """读取启动扫描的回看缓冲小时数（0..720），避免重启窗口内终态 Run 遗漏。"""
    raw = os.getenv(
        "ATHENA_MEMORY_SCAN_LOOKBACK_HOURS",
        str(DEFAULT_MEMORY_SCAN_LOOKBACK_HOURS),
    ).strip()
    try:
        value = int(raw)
    except ValueError as error:
        raise ValueError("athena_memory_scan_lookback_hours_invalid") from error
    if not 0 <= value <= MAX_MEMORY_SCAN_LOOKBACK_HOURS:
        raise ValueError("athena_memory_scan_lookback_hours_invalid")
    return value


async def _consume(
    worker: MemoryWorker,
    *,
    idle_sleep_seconds: float,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """持续消费任务；数据库暂不可用时做有界抖动退避。"""
    consecutive_failures = 0
    recover_before_claim = False
    while True:
        try:
            if recover_before_claim:
                # 上一轮领取后收敛失败时，run_once 已退出；先恢复普通 running
                # 再重新领取，避免任务在单进程内永久停在 running。
                await worker.recover_running_jobs()
                recover_before_claim = False
            claimed = await worker.run_once()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if not _is_recoverable_worker_error(error):
                raise
            recover_before_claim = True
            consecutive_failures += 1
            ceiling = min(30.0, 0.5 * (2 ** min(consecutive_failures - 1, 6)))
            retry_after = random.uniform(0.0, ceiling)
            logger.warning(
                "memory_worker_database_retry error_type=%s retry_after=%.2f",
                type(error).__name__,
                retry_after,
            )
            await sleep(retry_after)
            continue
        consecutive_failures = 0
        if not claimed:
            await sleep(idle_sleep_seconds)


def _is_recoverable_worker_error(error: Exception) -> bool:
    """只重试数据库连接中断、暂不可用和系统级连接错误。"""
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


async def run_worker() -> None:
    """装配资源、验证默认模型并运行 Memory Worker。"""
    bundle = MemoryWorkerBundle()
    failure: BaseException | None = None
    try:
        build_memory_worker_dependencies(bundle)
        config = bundle.config
        counter = bundle.counter
        embedding = bundle.embedding
        provider = bundle.provider
        worker = bundle.worker
        assert (
            config is not None
            and counter is not None
            and embedding is not None
            and provider is not None
            and worker is not None
        )
        await embedding.start()
        await counter.start()
        await worker.validate_runtime()
        # 单 Worker 启动时先恢复遗留 running；仓储明确排除 control 回执。
        await worker.recover_running_jobs()
        # 启动扫描边界向前回看缓冲窗口，覆盖进程重启或升级期间漏唤醒的终态 pending Run。
        lookback_hours = _scan_lookback_hours()
        worker.set_scan_boundary(utc_now() - timedelta(hours=lookback_hours))
        # 直接复用组合根已校验的 idle_sleep_seconds，避免重复解析环境变量。
        await _consume(
            worker,
            idle_sleep_seconds=config.idle_sleep_seconds,
        )
    except BaseException as error:
        failure = error
    finally:
        if bundle.provider is not None:
            try:
                await bundle.provider.aclose()
            except BaseException as error:
                logger.error(
                    "memory_worker_provider_cleanup_failed exception_type=%s",
                    type(error).__name__,
                )
                if failure is None:
                    failure = error
        if bundle.embedding is not None:
            try:
                await bundle.embedding.aclose()
            except BaseException as error:
                logger.error(
                    "memory_worker_embedding_cleanup_failed exception_type=%s",
                    type(error).__name__,
                )
                if failure is None:
                    failure = error
        if bundle.counter is not None:
            try:
                bundle.counter.close()
            except BaseException as error:
                logger.error(
                    "memory_worker_counter_cleanup_failed exception_type=%s",
                    type(error).__name__,
                )
                if failure is None:
                    failure = error
        if bundle.database is not None:
            try:
                await bundle.database.dispose()
            except BaseException as error:
                logger.error(
                    "memory_worker_database_cleanup_failed exception_type=%s",
                    type(error).__name__,
                )
                if failure is None:
                    failure = error
    if failure is not None:
        raise failure


def main() -> None:
    """配置日志并以正常信号语义运行异步入口。"""
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
