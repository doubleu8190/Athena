"""独立 Run Worker 进程入口。"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from contextlib import suppress

from app.run.context import prepare_run_tokenizers
from bootstrap.run_worker import RunWorkerBundle, build_run_worker_dependencies

logger = logging.getLogger(__name__)


async def run_worker_process(
    *,
    database_url: str | None = None,
    stop_event: asyncio.Event | None = None,
) -> None:
    """组装 Worker 并在停止信号后依次清理已创建的资源。"""
    # 外部资源由 bundle 持有，finally 中只清理真正创建成功的部分；
    # failure 暂存首个异常，确保清理流程执行完毕后再原样抛出。
    bundle = RunWorkerBundle()
    worker_task: asyncio.Task[None] | None = None
    stop_task: asyncio.Task[bool] | None = None
    # 调用方可注入停止事件（如 _main 的信号处理），未注入时自行创建。
    local_stop_event = stop_event or asyncio.Event()
    failure: BaseException | None = None
    try:
        # tokenizer 缺失时的下载仅允许在启动阶段限时预热，不能阻塞活跃 Worker。
        await prepare_run_tokenizers()
        # 资源构造全部收敛在组合根；构造中途失败时 bundle 只保留已完成的资源。
        build_run_worker_dependencies(bundle, database_url=database_url)
        # 本地模型必须在领取任务前启动，避免首个 Run 撞上冷加载。
        if bundle.query_services is not None:
            await bundle.query_services.start()
        worker = bundle.worker
        assert worker is not None
        # 并行等待 Worker 运行与停止信号，任一先结束就进入收尾流程。
        worker_task = asyncio.create_task(worker.run())
        stop_task = asyncio.create_task(local_stop_event.wait())
        done, _ = await asyncio.wait((worker_task, stop_task), return_when=asyncio.FIRST_COMPLETED)
        if worker_task in done:
            # Worker 自行结束通常是内部异常，先 await 取出并上抛。
            await worker_task
        # 停止信号先到：只请求停止领取新任务，等待在途 Run 执行完成后退出。
        await worker.stop()
        await worker_task
        # 正常路径已不再需要停止等待任务，取消并吞掉取消异常。
        stop_task.cancel()
        with suppress(asyncio.CancelledError):
            await stop_task
    except BaseException as error:
        # 包括 CancelledError 在内的所有异常都先暂存，保证 finally 完整清理后再抛出。
        failure = error
    finally:
        # 按依赖逆序清理，每步独立捕获异常，单个清理失败不阻断后续步骤；
        # 只记录首个失败，函数结束前统一抛出。
        if stop_task is not None and not stop_task.done():
            stop_task.cancel()
            with suppress(asyncio.CancelledError):
                await stop_task
        if bundle.worker is not None:
            # stop 幂等：正常路径已调用过，此处兜底覆盖提前异常退出的情况。
            await bundle.worker.stop()
        # 等待 Worker 的 TaskGroup 收敛；清理阶段的异常只记录，不覆盖原始 failure。
        if worker_task is not None:
            try:
                await worker_task
            except BaseException as error:
                logger.error(
                    "run_worker_shutdown_failed exception_type=%s",
                    type(error).__name__,
                )
                if failure is None:
                    failure = error
        # 依次关闭 LLM 客户端、查询模型、Redis 连接与数据库连接池。
        if bundle.provider is not None:
            try:
                await bundle.provider.aclose()
            except BaseException as error:
                logger.error(
                    "run_worker_llm_cleanup_failed exception_type=%s",
                    type(error).__name__,
                )
                if failure is None:
                    failure = error
        if bundle.query_services is not None:
            try:
                await bundle.query_services.aclose()
            except BaseException as error:
                logger.error(
                    "run_worker_query_services_cleanup_failed exception_type=%s",
                    type(error).__name__,
                )
                if failure is None:
                    failure = error
        if bundle.run_transport is not None:
            try:
                await bundle.run_transport.aclose()
            except BaseException as error:
                logger.error(
                    "run_worker_redis_cleanup_failed exception_type=%s",
                    type(error).__name__,
                )
                if failure is None:
                    failure = error
        if bundle.database is not None:
            try:
                await bundle.database.dispose()
            except BaseException as error:
                logger.error(
                    "run_worker_database_cleanup_failed exception_type=%s",
                    type(error).__name__,
                )
                if failure is None:
                    failure = error
    # 清理完成后重新抛出首个异常，保留原始失败原因（含取消语义）。
    if failure is not None:
        raise failure


async def _main() -> None:
    """安装进程信号并运行独立 Worker 生命周期。"""
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signal_name, stop_event.set)
    await run_worker_process(stop_event=stop_event)


if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("ATHENA_LOG_LEVEL", "INFO"))
    asyncio.run(_main())


__all__ = ["run_worker_process"]
