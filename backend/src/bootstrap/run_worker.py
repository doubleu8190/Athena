"""Run Worker 进程的组合根：集中装配仓储、LLM、Redis 与 Worker。"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from adapter.auth.repository import SqlAlchemyAuthRepository
from adapter.file import MinioFileObjectStore, SqlAlchemySessionFileRepository
from adapter.llm import OpenAIChatCompletionsAdapter
from adapter.llm.repository import SqlAlchemyModelProfileRepository
from adapter.mcp.repository import SqlAlchemyMcpRepository
from adapter.message.repository import SqlAlchemyMessageRepository
from adapter.project.repository import SqlAlchemyProjectRepository
from adapter.run.redis_transport import RedisRunTransport
from adapter.run.repository import SqlAlchemyRunRepository
from adapter.session import SqlAlchemySessionRepository
from adapter.skill import SqlAlchemySkillRepository
from adapter.skill.execution import PythonScriptExecutor
from adapter.subagent import SqlAlchemySubagentRepository
from adapter.system_prompt import SqlAlchemySystemPromptRepository
from app.file import SessionFileService
from app.project.service import ProjectService
from app.run import (
    RunExecutor,
    RunWorker,
    worker_config_from_environment,
)
from app.skill import DependencyChecker, SkillService
from app.skill.execution_service import SkillExecutionService
from bootstrap.config import env_flag, memory_enabled_from_env
from bootstrap.database import build_database
from bootstrap.redis import build_run_transport
from bootstrap.tool import (
    QueryServiceBundle,
    build_mcp_executor,
    build_query_services,
    build_run_tool_runtime,
)
from domain.file import FileObjectStore
from infrastructure.database import Database
from infrastructure.security.mcp_secrets import McpSecretCipher


@dataclass(slots=True)
class RunWorkerBundle:
    """Run Worker 构造过程中已创建、需要由入口清理的资源。"""

    database: Database | None = None
    provider: OpenAIChatCompletionsAdapter | None = None
    run_transport: RedisRunTransport | None = None
    query_services: QueryServiceBundle | None = None
    file_object_store: FileObjectStore | None = None
    skill_execution: SkillExecutionService | None = None
    worker: RunWorker | None = None


_FILE_STORAGE_SETTING_NAMES = (
    "ATHENA_FILE_MINIO_ENDPOINT",
    "ATHENA_FILE_MINIO_ACCESS_KEY",
    "ATHENA_FILE_MINIO_SECRET_KEY",
    "ATHENA_FILE_MINIO_BUCKET",
)


def _files_enabled() -> bool:
    """Worker 使用独立文件配置，不依赖知识库或嵌入模型开关。"""
    if "ATHENA_FILES_ENABLED" in os.environ:
        return env_flag("ATHENA_FILES_ENABLED", default=False)
    return any(os.getenv(name, "").strip() for name in _FILE_STORAGE_SETTING_NAMES)


def _bounded_env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    """读取有界整数配置，空值回退默认，非法值在 Worker 启动时明确失败。"""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError as error:
        raise ValueError(f"{name.lower()}_invalid") from error
    if isinstance(value, bool) or not minimum <= value <= maximum:
        raise ValueError(f"{name.lower()}_invalid")
    return value


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
    profiles = SqlAlchemyModelProfileRepository(database.session_factory, secret_cipher)
    projects_repository = SqlAlchemyProjectRepository(database.session_factory)
    messages = SqlAlchemyMessageRepository(database.session_factory)
    system_prompts = SqlAlchemySystemPromptRepository(database.session_factory)
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
    memory_lifecycle_enabled = memory_enabled_from_env()
    repository = (
        SqlAlchemyRunRepository(
            database.session_factory,
            messages,
            memory_wakeup=query_services.memory_lifecycle,
            memory_lifecycle_enabled=True,
        )
        if memory_lifecycle_enabled
        else SqlAlchemyRunRepository(database.session_factory, messages)
    )
    skills = None
    session_files = None
    skill_execution = None
    if _files_enabled():
        object_store = MinioFileObjectStore.from_environment(deferred=True)
        bundle.file_object_store = object_store
        skill_repository = SqlAlchemySkillRepository(database.session_factory)
        python_executable = Path(os.getenv("ATHENA_SKILL_PYTHON_EXECUTABLE", sys.executable))
        skills = SkillService(
            skill_repository,
            object_store,
            auth_repository,
            dependency_checker=DependencyChecker(python_executable=python_executable),
        )
        file_repository = SqlAlchemySessionFileRepository(database.session_factory)
        session_files = SessionFileService(
            file_repository,
            object_store,
        )
        temp_root = Path(
            os.getenv(
                "ATHENA_SKILL_TEMP_ROOT",
                str(Path(sys.prefix) / "tmp" / "athena-skill-executions"),
            )
        )
        skill_execution = SkillExecutionService(
            skills=skills,
            files=session_files,
            repository=skill_repository,
            object_store=object_store,
            runner=PythonScriptExecutor(python_executable),
            temp_root=temp_root,
        )
        bundle.skill_execution = skill_execution
    subagents_repository = SqlAlchemySubagentRepository(database.session_factory)
    tool_runtime = build_run_tool_runtime(
        auth=auth_repository,
        sessions=session_repository,
        mcp_catalog=mcp_catalog,
        mcp_executor=mcp_executor,
        query_services=query_services,
        skills=skills,
        session_files=session_files,
        skill_execution=skill_execution,
        subagents=subagents_repository,
    )
    provider = OpenAIChatCompletionsAdapter()
    bundle.provider = provider
    # Redis 未配置时保持为空，后续以 NullRunProgressPublisher 降级运行，
    # 仅失去实时推送，不影响 Run 的持久化与状态流转。
    run_transport = build_run_transport()
    bundle.run_transport = run_transport
    # 接通 compose.yml 已声明的 Run 记忆上下文预算与单 scope 条目上限，启动期有界校验。
    memory_personal_tokens = _bounded_env_int(
        "ATHENA_MEMORY_CONTEXT_PERSONAL_TOKENS",
        1_000,
        minimum=1,
        maximum=100_000,
    )
    memory_project_tokens = _bounded_env_int(
        "ATHENA_MEMORY_CONTEXT_PROJECT_TOKENS",
        2_000,
        minimum=1,
        maximum=100_000,
    )
    memory_per_scope_limit = _bounded_env_int(
        "ATHENA_MEMORY_CONTEXT_PER_SCOPE_LIMIT",
        200,
        minimum=1,
        maximum=200,
    )
    # Executor 负责执行单个 Run；Worker 负责领取、续约并驱动执行。
    executor = RunExecutor(
        repository,
        profiles,
        provider,
        system_prompts=system_prompts,
        publisher=run_transport,
        config=config,
        tool_runtime=tool_runtime,
        memory_lifecycle=query_services.memory_lifecycle,
        memory_personal_token_budget=memory_personal_tokens,
        memory_project_token_budget=memory_project_tokens,
        memory_per_scope_limit=memory_per_scope_limit,
        subagents=subagents_repository,
    )
    bundle.worker = RunWorker(repository, executor, config, publisher=run_transport)


__all__ = ["RunWorkerBundle", "build_run_worker_dependencies"]
