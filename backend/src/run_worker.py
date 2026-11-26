"""独立 Run Worker 进程入口。"""

from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Callable
from contextlib import suppress
from typing import Any

from app.run.context import prepare_run_tokenizers
from bootstrap.config import LoggingSettings
from bootstrap.lifecycle import close_resources_lifo
from bootstrap.run_worker import RunWorkerBundle, build_run_worker_dependencies

logger = logging.getLogger(__name__)


async def run_worker_process(
    *,
    database_url: str | None = None,
    stop_event: asyncio.Event | None = None,
) -> None:
    """组装 Worker 并在停止信号后经 AsyncExitStack 严格逆序清理已创建的资源。"""
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
        if bundle.file_object_store is not None:
            await bundle.file_object_store.start()
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
        if stop_task is not None and not stop_task.done():
            stop_task.cancel()
            with suppress(asyncio.CancelledError):
                await stop_task
        if bundle.worker is not None:
            # stop 幂等：正常路径已调用过，此处兜底覆盖提前异常退出的情况。
            await bundle.worker.stop()
        # 按创建顺序登记，经 AsyncExitStack 严格逆序释放：
        # worker_task -> mcp_executor -> skill_package_cache -> file_store ->
        # provider -> query_services -> storage_client -> run_transport -> database。
        cleanups: list[tuple[str, Callable[[], Any]]] = []
        if bundle.database is not None:
            database = bundle.database
            cleanups.append(("run_worker_database_cleanup_failed", lambda: database.dispose()))
        if bundle.run_transport is not None:
            run_transport = bundle.run_transport
            cleanups.append(("run_worker_redis_cleanup_failed", lambda: run_transport.aclose()))
        if bundle.storage_client is not None:
            storage_client = bundle.storage_client
            cleanups.append(
                ("run_worker_shared_storage_cleanup_failed", lambda: storage_client.aclose())
            )
        if bundle.query_services is not None:
            query_services = bundle.query_services
            cleanups.append(
                ("run_worker_query_services_cleanup_failed", lambda: query_services.aclose())
            )
        if bundle.provider is not None:
            provider = bundle.provider
            cleanups.append(("run_worker_llm_cleanup_failed", lambda: provider.aclose()))
        if bundle.file_object_store is not None:
            file_store = bundle.file_object_store
            cleanups.append(("run_worker_file_storage_cleanup_failed", lambda: file_store.aclose()))
        if bundle.skill_package_cache is not None:
            package_cache = bundle.skill_package_cache
            cleanups.append(
                ("run_worker_skill_cache_cleanup_failed", lambda: package_cache.aclose())
            )
        if bundle.mcp_executor is not None:
            mcp_executor = bundle.mcp_executor
            cleanups.append(("run_worker_mcp_cleanup_failed", lambda: mcp_executor.aclose()))
        # 仅当 worker_task 尚未结束时在收尾阶段等待其收敛；
        # 若已在 try 块中因内部异常结束并被 await 取出，跳过二次 await 以避免误导性清理错误日志。
        if worker_task is not None and not worker_task.done():
            active_task = worker_task
            cleanups.append(("run_worker_shutdown_failed", lambda: active_task))
        await close_resources_lifo(
            cleanups,
            log=logger,
            reraise=True,
            initial_error=failure,
        )


async def _main() -> None:
    """安装进程信号并运行独立 Worker 生命周期。"""
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signal_name, stop_event.set)
    await run_worker_process(stop_event=stop_event)


if __name__ == "__main__":
    logging.basicConfig(level=LoggingSettings.from_environment().level)
    asyncio.run(_main())


__all__ = ["run_worker_process"]
