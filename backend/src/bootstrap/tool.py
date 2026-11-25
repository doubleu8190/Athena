"""API 与 Run Worker 共享的组合根：在此组装 adapter/infrastructure 资源与查询用例。

应用层只消费领域端口；具体仓储、模型资源与外部客户端的选择全部收敛在本模块，
保证两个进程使用同一套环境变量和依赖装配规则。
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from adapter.access import SqlAlchemyAccessControlRepository
from adapter.knowledge.repository import KnowledgeRepository
from app.access import AccessPolicyService
from app.file.service import SessionFileService
from app.knowledge.service import KnowledgeService
from app.preference import PreferenceService
from app.project.knowledge import ProjectKnowledgeRepository, ProjectKnowledgeService
from app.project.service import ProjectService
from app.skill.execution_service import SkillExecutionService
from app.skill.service import SkillService
from app.tool.runtime import RunToolRuntimeService
from bootstrap.config import env_csv_set, env_flag
from bootstrap.knowledge import (
    build_knowledge_models,
    build_knowledge_original_store,
)
from domain.auth import CurrentSessionReader
from domain.knowledge.ports import (
    KnowledgeRepositoryPort,
    KnowledgeUploadSlotRepository,
    OriginalStore,
)
from domain.mcp import McpToolExecutor, McpToolRuntimeReader
from domain.session import SessionRepository
from domain.subagent import SubagentRepository
from infrastructure.mcp.execution import McpToolCallClient

logger = logging.getLogger(__name__)


def _bounded_env_float(
    name: str,
    default: float,
    *,
    minimum: float,
    maximum: float,
) -> float:
    """读取有界浮点环境变量，非法值、NaN/Inf 或越界在启动阶段直接失败。"""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw.strip())
    except ValueError as error:
        raise ValueError(f"{name.lower()}_invalid") from error
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f"{name.lower()}_invalid")
    return value


@dataclass(slots=True)
class QueryServiceBundle:
    """知识库查询服务及其进程级无状态资源。"""

    knowledge: KnowledgeService | None
    project_knowledge: ProjectKnowledgeService | None
    # 对外只暴露领域端口，应用层不需要知道具体仓储实现。
    knowledge_repository: KnowledgeRepositoryPort | None
    knowledge_original_store: OriginalStore | None
    knowledge_enabled: bool
    _startables: tuple[Any, ...]
    _closables: tuple[Any, ...]
    knowledge_upload_slots: KnowledgeUploadSlotRepository | None = None

    async def start(self) -> None:
        """在首个请求前预热已装配的外部存储、无状态客户端与中文分词词典。"""
        for resource in self._startables:
            start = getattr(resource, "start", None)
            if callable(start):
                await start()
                continue
            warmup = getattr(resource, "warmup", None)
            if callable(warmup):
                await asyncio.to_thread(warmup)

    async def aclose(self) -> None:
        """按逆序独立释放所有进程级查询资源。"""
        first_error: BaseException | None = None
        for resource in reversed(self._closables):
            close = getattr(resource, "aclose", None)
            try:
                if close is not None:
                    await close()
                    continue
                close = getattr(resource, "close", None)
                if close is not None:
                    close()
            except BaseException as error:
                logger.warning(
                    "query_service_cleanup_failed exception_type=%s",
                    type(error).__name__,
                )
                if first_error is None:
                    first_error = error
        if first_error is not None:
            raise first_error


def build_query_services(
    sessions: async_sessionmaker[AsyncSession],
    *,
    projects: ProjectService,
    knowledge_service: KnowledgeService | None = None,
    access_policy: AccessPolicyService | None = None,
) -> QueryServiceBundle:
    """按统一环境变量为 API 与 Run Worker 装配知识库查询服务与实时权限引擎。"""
    knowledge_enabled = knowledge_service is not None or env_flag(
        "ATHENA_KNOWLEDGE_ENABLED", default=False
    )
    startables: list[Any] = []
    closables: list[Any] = []
    knowledge = knowledge_service
    resolved_access_policy = (
        access_policy
        if access_policy is not None
        else AccessPolicyService(SqlAlchemyAccessControlRepository(sessions))
    )
    # 组合根是唯一允许构造具体适配器的地方；bundle 只暴露领域端口。
    knowledge_repository: KnowledgeRepositoryPort | None = None
    knowledge_upload_slots: KnowledgeUploadSlotRepository | None = None
    # 项目知识关联需要额外的仓储能力，构造期单独持有具体实现，不进入公开契约。
    project_knowledge_repository: ProjectKnowledgeRepository | None = None
    knowledge_originals: OriginalStore | None = None
    if knowledge_enabled and knowledge is None:
        # 先校验并构造统一对象存储，再构造无状态远程 Embedding 与 tiktoken 计数资源。
        minio_originals = build_knowledge_original_store()
        knowledge_originals = minio_originals
        models = build_knowledge_models()
        concrete_repository = KnowledgeRepository(sessions)
        knowledge_repository = concrete_repository
        knowledge_upload_slots = concrete_repository
        project_knowledge_repository = concrete_repository
        semantic_min_score = _bounded_env_float(
            "ATHENA_KNOWLEDGE_SEMANTIC_MIN_SCORE",
            0.35,
            minimum=0.0,
            maximum=1.0,
        )
        knowledge = KnowledgeService(
            concrete_repository,
            models.tokenizer,
            models.embedding,
            knowledge_originals,
            models.counter,
            access_policy=resolved_access_policy,
            profile_id=models.profile_id,
            semantic_min_score=semantic_min_score,
        )
        # 启动顺序沿用存储 → 计数 → 远程向量客户端 → 中文分词词典预热，关闭时按相反依赖顺序释放。
        startables.extend((minio_originals, models.counter, models.embedding, models.tokenizer))
        closables.extend((minio_originals, models.embedding, models.counter))
    elif knowledge is not None and knowledge_repository is None:
        concrete_repository = KnowledgeRepository(sessions)
        knowledge_repository = concrete_repository
        knowledge_upload_slots = concrete_repository
        project_knowledge_repository = concrete_repository
    project_knowledge = (
        None
        if not knowledge_enabled or knowledge is None or project_knowledge_repository is None
        else ProjectKnowledgeService(
            projects,
            project_knowledge_repository,
            access_policy=resolved_access_policy,
        )
    )
    return QueryServiceBundle(
        knowledge=knowledge,
        project_knowledge=project_knowledge,
        knowledge_repository=knowledge_repository,
        knowledge_original_store=knowledge_originals,
        knowledge_enabled=knowledge_enabled,
        _startables=tuple(startables),
        _closables=tuple(closables),
        knowledge_upload_slots=knowledge_upload_slots,
    )


def build_mcp_executor(
    *,
    cleanup_timeout_seconds: float = 10.0,
) -> McpToolCallClient:
    """Create the single-call MCP executor with the deployment transport limits."""
    allowed_private_hosts = env_csv_set("ATHENA_MCP_ALLOWED_PRIVATE_HOSTS")
    stdio_names = env_csv_set("ATHENA_MCP_STDIO_BASE_ENV_NAMES")
    base_environment = {name: os.environ[name] for name in stdio_names if name in os.environ}
    return McpToolCallClient(
        allowed_private_hosts=allowed_private_hosts,
        stdio_base_environment=base_environment,
        stdio_allowed_environment_names=stdio_names,
        cleanup_timeout_seconds=cleanup_timeout_seconds,
    )


def build_run_tool_runtime(
    *,
    auth: CurrentSessionReader,
    sessions: SessionRepository,
    mcp_catalog: McpToolRuntimeReader,
    mcp_executor: McpToolExecutor,
    query_services: QueryServiceBundle,
    preferences: PreferenceService | None = None,
    skills: SkillService | None = None,
    session_files: SessionFileService | None = None,
    skill_execution: SkillExecutionService | None = None,
    subagents: SubagentRepository | None = None,
    access_policy: AccessPolicyService | None = None,
) -> RunToolRuntimeService:
    """绑定查询服务、轻量偏好服务、统一权限引擎与 MCP 端口到 Run 工具运行时。"""
    return RunToolRuntimeService(
        auth=auth,
        sessions=sessions,
        mcp_catalog=mcp_catalog,
        mcp_executor=mcp_executor,
        knowledge=query_services.knowledge,
        project_knowledge=query_services.project_knowledge,
        preferences=preferences,
        skills=skills,
        session_files=session_files,
        skill_execution=skill_execution,
        subagents=subagents,
        access_policy=access_policy,
    )


__all__ = [
    "QueryServiceBundle",
    "build_mcp_executor",
    "build_query_services",
    "build_run_tool_runtime",
]
