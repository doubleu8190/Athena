"""API 与 Run Worker 共享的组合根：在此组装 adapter/infrastructure 资源与查询用例。

应用层只消费领域端口；具体仓储、模型资源与外部客户端的选择全部收敛在本模块，
保证两个进程使用同一套环境变量和依赖装配规则。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from adapter.knowledge.repository import KnowledgeRepository
from adapter.memory.repository import MemoryRepository
from app.file.service import SessionFileService
from app.knowledge.service import KnowledgeService
from app.memory.service import MemoryService
from app.project.knowledge import ProjectKnowledgeRepository, ProjectKnowledgeService
from app.project.service import ProjectService
from app.skill.execution_service import SkillExecutionService
from app.skill.service import SkillService
from app.tool.runtime import RunToolRuntimeService
from bootstrap.config import env_csv_set, env_flag, memory_enabled_from_env
from bootstrap.knowledge import build_knowledge_models, build_knowledge_original_store
from domain.auth import CurrentSessionReader
from domain.knowledge.ports import (
    KnowledgeRepositoryPort,
    KnowledgeUploadSlotRepository,
    OriginalStore,
)
from domain.mcp import McpToolExecutor, McpToolRuntimeReader
from domain.memory.ports import MemoryLifecycleRepositoryPort
from domain.session import SessionRepository
from domain.subagent import SubagentRepository
from infrastructure.embedding import ApproximateTokenCounter, LocalEmbedding
from infrastructure.embedding.profile import embedding_profile_id
from infrastructure.mcp.execution import McpToolCallClient
from infrastructure.memory import LocalReranker

logger = logging.getLogger(__name__)


def _profile_id(model: str, revision: str) -> str:
    return embedding_profile_id(model, revision)


@dataclass(slots=True)
class QueryServiceBundle:
    """Memory and knowledge services with their process-scoped model resources."""

    memory: MemoryService | None
    knowledge: KnowledgeService | None
    project_knowledge: ProjectKnowledgeService | None
    # 对外只暴露领域端口，应用层不需要知道具体仓储实现。
    knowledge_repository: KnowledgeRepositoryPort | None
    knowledge_original_store: OriginalStore | None
    memory_enabled: bool
    knowledge_enabled: bool
    _startables: tuple[Any, ...]
    _closables: tuple[Any, ...]
    knowledge_upload_slots: KnowledgeUploadSlotRepository | None = None
    memory_lifecycle: MemoryLifecycleRepositoryPort | None = None

    async def start(self) -> None:
        """Start lazily loaded local models before the first query."""
        for resource in self._startables:
            await resource.start()

    async def aclose(self) -> None:
        """Close every created model resource independently."""
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
    memory_service: MemoryService | None = None,
    knowledge_service: KnowledgeService | None = None,
) -> QueryServiceBundle:
    """Build memory/knowledge query services from the same environment in both processes."""
    # 统一记忆开关：兼容 ATHENA_MEMORY_ENABLED 与 ATHENA_MEMORY_LIFECYCLE_ENABLED。
    memory_enabled = memory_service is not None or memory_enabled_from_env()
    knowledge_enabled = knowledge_service is not None or env_flag(
        "ATHENA_KNOWLEDGE_ENABLED", default=False
    )
    startables: list[Any] = []
    closables: list[Any] = []
    memory = memory_service
    memory_repository: MemoryRepository | None = None
    knowledge = knowledge_service
    # 组合根是唯一允许构造具体适配器的地方；bundle 只暴露领域端口。
    knowledge_repository: KnowledgeRepositoryPort | None = None
    knowledge_upload_slots: KnowledgeUploadSlotRepository | None = None
    # 项目知识关联需要额外的仓储能力，构造期单独持有具体实现，不进入公开契约。
    project_knowledge_repository: ProjectKnowledgeRepository | None = None
    knowledge_originals: OriginalStore | None = None
    if memory_enabled and memory is None:
        embedding_model = os.getenv("ATHENA_MEMORY_EMBEDDING_MODEL", "BAAI/bge-m3")
        embedding_revision = os.getenv(
            "ATHENA_MEMORY_EMBEDDING_REVISION",
            "5617a9f61b028005a4858fdac845db406aefb181",
        )
        reranker_model = os.getenv("ATHENA_MEMORY_RERANKER_MODEL", "BAAI/bge-reranker-v2-m3")
        reranker_revision = os.getenv(
            "ATHENA_MEMORY_RERANKER_REVISION",
            "953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e",
        )
        # QueryServiceBundle 的记忆召回仅需粗略 token 预算估算，
        # 使用轻量计数器避免常驻加载额外 AutoTokenizer。
        embedding = LocalEmbedding(
            embedding_model,
            embedding_revision,
            os.getenv("ATHENA_MEMORY_EMBEDDING_DEVICE", "cpu"),
        )
        reranker = LocalReranker(
            reranker_model,
            reranker_revision,
            os.getenv("ATHENA_MEMORY_RERANKER_DEVICE", "cpu"),
        )
        counter = ApproximateTokenCounter()
        memory_repository = MemoryRepository(sessions)
        memory = MemoryService(
            memory_repository,
            embedding,
            reranker,
            counter,
            profile_id=_profile_id(embedding_model, embedding_revision),
            lifecycle=memory_repository,
            control_source=memory_repository,
        )
        startables.extend((embedding, reranker))
        closables.extend((embedding, reranker))
    if knowledge_enabled and knowledge is None:
        # 先校验并构造原件存储，再加载模型资源，保持原有启动顺序。
        minio_originals = build_knowledge_original_store()
        knowledge_originals = minio_originals
        models = build_knowledge_models()
        concrete_repository = KnowledgeRepository(sessions)
        knowledge_repository = concrete_repository
        knowledge_upload_slots = concrete_repository
        project_knowledge_repository = concrete_repository
        knowledge = KnowledgeService(
            concrete_repository,
            models.tokenizer,
            models.embedding,
            knowledge_originals,
            models.counter,
            profile_id=models.profile_id,
        )
        # 启动顺序沿用存储 → 计数 → 向量模型，关闭时按相反依赖顺序释放。
        startables.extend((minio_originals, models.counter, models.embedding))
        closables.extend((minio_originals, models.embedding, models.counter))
    elif knowledge is not None and knowledge_repository is None:
        concrete_repository = KnowledgeRepository(sessions)
        knowledge_repository = concrete_repository
        knowledge_upload_slots = concrete_repository
        project_knowledge_repository = concrete_repository
    project_knowledge = (
        None
        if not knowledge_enabled or knowledge is None or project_knowledge_repository is None
        else ProjectKnowledgeService(projects, project_knowledge_repository)
    )
    return QueryServiceBundle(
        memory=memory,
        knowledge=knowledge,
        project_knowledge=project_knowledge,
        knowledge_repository=knowledge_repository,
        knowledge_original_store=knowledge_originals,
        memory_enabled=memory_enabled,
        knowledge_enabled=knowledge_enabled,
        _startables=tuple(startables),
        _closables=tuple(closables),
        knowledge_upload_slots=knowledge_upload_slots,
        memory_lifecycle=memory_repository if memory_enabled else None,
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
    skills: SkillService | None = None,
    session_files: SessionFileService | None = None,
    skill_execution: SkillExecutionService | None = None,
    subagents: SubagentRepository | None = None,
) -> RunToolRuntimeService:
    """Bind the shared query services and MCP ports to the Run tool runtime."""
    return RunToolRuntimeService(
        auth=auth,
        sessions=sessions,
        mcp_catalog=mcp_catalog,
        mcp_executor=mcp_executor,
        memory=query_services.memory,
        memory_enabled=query_services.memory_enabled,
        knowledge=query_services.knowledge,
        project_knowledge=query_services.project_knowledge,
        skills=skills,
        session_files=session_files,
        skill_execution=skill_execution,
        subagents=subagents,
    )


__all__ = [
    "QueryServiceBundle",
    "build_mcp_executor",
    "build_query_services",
    "build_run_tool_runtime",
]
