"""Run Worker 进程的组合根：集中装配仓储、LLM、Redis 与 Worker。"""

from __future__ import annotations

import os
from dataclasses import dataclass

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
from bootstrap.database import build_database
from bootstrap.redis import build_run_transport
from bootstrap.tool import (
    QueryServiceBundle,
    build_mcp_executor,
    build_query_services,
    build_run_tool_runtime,
)
from infrastructure.database import Database
from infrastructure.security.mcp_secrets import McpSecretCipher


@dataclass(slots=True)
class RunWorkerBundle:
    """Run Worker 构造过程中已创建、需要由入口清理的资源。"""

    database: Database | None = None
    provider: OpenAIChatCompletionsAdapter | None = None
    run_transport: RedisRunTransport | None = None
    query_services: QueryServiceBundle | None = None
    worker: RunWorker | None = None


def build_run_worker_dependencies(
    bundle: RunWorkerBundle,
    *,
    database_url: str | None = None,
) -> None:
    """按依赖顺序组装 Run Worker，并把资源逐项写入 bundle。

    query_services 的模型启动是异步的，由入口在调用本函数后执行。构造中途
    失败时，入口 finally 只清理 bundle 中已填充的字段，因此这里每完成一个
    资源就立即写回 bundle。
    """
    # 数据库地址优先使用入参，其次读取环境变量；缺失时先于其他配置解析直接拒绝。
    configured_url = database_url or os.getenv("ATHENA_DATABASE_URL", "").strip()
    if not configured_url:
        raise RuntimeError("run_worker_database_required")
    # 加载有界并发、租约与轮询参数，非法配置会在构造配置对象时抛出。
    config = worker_config_from_environment()
    # MCP 凭据解封与 API 使用同一密钥；缺失或损坏时拒绝启动，不能静默禁用工具。
    secret_cipher = McpSecretCipher.from_environment()
    database = build_database(configured_url)
    bundle.database = database
    profiles = SqlAlchemyModelProfileRepository(database.session_factory)
    projects_repository = SqlAlchemyProjectRepository(database.session_factory)
    messages = SqlAlchemyMessageRepository(database.session_factory)
    repository = SqlAlchemyRunRepository(database.session_factory, messages)
    auth_repository = SqlAlchemyAuthRepository(database.session_factory)
    session_repository = SqlAlchemySessionRepository(database.session_factory)
    mcp_catalog = SqlAlchemyMcpRepository(database.session_factory, secret_cipher)
    projects = ProjectService(projects_repository)
    mcp_executor = build_mcp_executor(cleanup_timeout_seconds=config.tool_cleanup_seconds)
    query_services = build_query_services(
        database.session_factory,
        projects=projects,
    )
    bundle.query_services = query_services
    tool_runtime = build_run_tool_runtime(
        auth=auth_repository,
        sessions=session_repository,
        mcp_catalog=mcp_catalog,
        mcp_executor=mcp_executor,
        query_services=query_services,
    )
    provider = OpenAIChatCompletionsAdapter()
    bundle.provider = provider
    # Redis 未配置时保持为空，后续以 NullRunProgressPublisher 降级运行，
    # 仅失去实时推送，不影响 Run 的持久化与状态流转。
    run_transport = build_run_transport()
    bundle.run_transport = run_transport
    # Executor 负责执行单个 Run；Worker 负责领取、续约并驱动执行。
    executor = RunExecutor(
        repository,
        profiles,
        provider,
        publisher=run_transport or NullRunProgressPublisher(),
        config=config,
        tool_runtime=tool_runtime,
    )
    bundle.worker = RunWorker(repository, executor, config, publisher=run_transport)


__all__ = ["RunWorkerBundle", "build_run_worker_dependencies"]
