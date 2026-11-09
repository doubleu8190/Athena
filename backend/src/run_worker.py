"""独立 Run Worker 进程入口。"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from contextlib import suppress

from redis.asyncio import Redis

from adapter.auth.repository import SqlAlchemyAuthRepository
from adapter.llm import OpenAIChatCompletionsAdapter
from adapter.llm.repository import SqlAlchemyModelProfileRepository
from adapter.mcp.repository import SqlAlchemyMcpRepository
from adapter.message.repository import SqlAlchemyMessageRepository
from adapter.project.repository import SqlAlchemyProjectRepository
from adapter.run.redis_transport import RedisRunTransport
from adapter.run.repository import SqlAlchemyRunRepository
from adapter.session import SqlAlchemySessionRepository
from app.project.service import ProjectService
from app.run import (
    NullRunProgressPublisher,
    RunExecutor,
    RunWorker,
    worker_config_from_environment,
)
from app.run.context import prepare_run_tokenizers
from app.tool.dependencies import (
    QueryServiceBundle,
    build_mcp_executor,
    build_query_services,
    build_run_tool_runtime,
)
from infrastructure.database import Database
from infrastructure.security.mcp_secrets import McpSecretCipher

logger = logging.getLogger(__name__)


async def run_worker_process(
    *,
    database_url: str | None = None,
    stop_event: asyncio.Event | None = None,
) -> None:
    """组装 Worker 并在停止信号后依次清理已创建的资源。"""
    # 数据库地址优先使用入参，其次读取环境变量；缺失时直接拒绝启动。
    configured_url = database_url or os.getenv("ATHENA_DATABASE_URL", "").strip()
    if not configured_url:
        raise RuntimeError("run_worker_database_required")
    # 加载有界并发、租约与轮询参数，非法配置会在构造配置对象时抛出。
    config = worker_config_from_environment()
    # 外部资源先声明为空，finally 中只清理真正创建成功的部分；
    # failure 暂存首个异常，确保清理流程执行完毕后再原样抛出。
    database: Database | None = None
    provider: OpenAIChatCompletionsAdapter | None = None
    run_transport: RedisRunTransport | None = None
    query_services: QueryServiceBundle | None = None
    worker: RunWorker | None = None
    worker_task: asyncio.Task[None] | None = None
    stop_task: asyncio.Task[bool] | None = None
    # 调用方可注入停止事件（如 _main 的信号处理），未注入时自行创建。
    local_stop_event = stop_event or asyncio.Event()
    failure: BaseException | None = None
    try:
        # tokenizer 缺失时的下载仅允许在启动阶段限时预热，不能阻塞活跃 Worker。
        await prepare_run_tokenizers()
        # MCP 凭据解封与 API 使用同一密钥；缺失或损坏时拒绝启动，不能静默禁用工具。
        secret_cipher = McpSecretCipher.from_environment()
        # 按依赖顺序组装：数据库 → 仓储 → LLM 适配器 → 可选 Redis 传输。
        database = Database(configured_url)
        profiles = SqlAlchemyModelProfileRepository(database.session_factory)
        projects_repository = SqlAlchemyProjectRepository(database.session_factory)
        projects = ProjectService(projects_repository)
        messages = SqlAlchemyMessageRepository(database.session_factory)
        repository = SqlAlchemyRunRepository(database.session_factory, messages)
        auth_repository = SqlAlchemyAuthRepository(database.session_factory)
        session_repository = SqlAlchemySessionRepository(database.session_factory)
        mcp_catalog = SqlAlchemyMcpRepository(database.session_factory, secret_cipher)
        mcp_executor = build_mcp_executor(cleanup_timeout_seconds=config.tool_cleanup_seconds)
        query_services = build_query_services(
            database.session_factory,
            projects=projects,
        )
        await query_services.start()
        tool_runtime = build_run_tool_runtime(
            auth=auth_repository,
            sessions=session_repository,
            mcp_catalog=mcp_catalog,
            mcp_executor=mcp_executor,
            query_services=query_services,
        )
        provider = OpenAIChatCompletionsAdapter()
        # Redis 未配置时保持为空，后续以 NullRunProgressPublisher 降级运行，
        # 仅失去实时推送，不影响 Run 的持久化与状态流转。
        redis_url = os.getenv("ATHENA_REDIS_URL", "").strip()
        if redis_url:
            run_transport = RedisRunTransport(
                Redis.from_url(redis_url, decode_responses=False, socket_connect_timeout=1)
            )
        # Executor 负责执行单个 Run；Worker 负责领取、续约并驱动执行。
        executor = RunExecutor(
            repository,
            profiles,
            provider,
            publisher=run_transport or NullRunProgressPublisher(),
            config=config,
            tool_runtime=tool_runtime,
        )
        worker = RunWorker(repository, executor, config, publisher=run_transport)
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
        if worker is not None:
            # stop 幂等：正常路径已调用过，此处兜底覆盖提前异常退出的情况。
            await worker.stop()
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
        # 依次关闭 LLM 客户端、Redis 连接与数据库连接池。
        if provider is not None:
            try:
                await provider.aclose()
            except BaseException as error:
                logger.error(
                    "run_worker_llm_cleanup_failed exception_type=%s",
                    type(error).__name__,
                )
                if failure is None:
                    failure = error
        if query_services is not None:
            try:
                await query_services.aclose()
            except BaseException as error:
                logger.error(
                    "run_worker_query_services_cleanup_failed exception_type=%s",
                    type(error).__name__,
                )
                if failure is None:
                    failure = error
        if run_transport is not None:
            try:
                await run_transport.aclose()
            except BaseException as error:
                logger.error(
                    "run_worker_redis_cleanup_failed exception_type=%s",
                    type(error).__name__,
                )
                if failure is None:
                    failure = error
        if database is not None:
            try:
                await database.dispose()
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
