"""Shared composition helpers for API and Run Worker tool dependencies."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from adapter.knowledge.repository import KnowledgeRepository
from adapter.knowledge.storage import MinioOriginalStore
from app.knowledge.service import KnowledgeService
from app.memory.service import MemoryService
from app.project.knowledge import ProjectKnowledgeService
from app.project.service import ProjectService
from app.tool.runtime import RunToolRuntimeService
from domain.auth import CurrentSessionReader
from domain.knowledge.ports import OriginalStore
from domain.mcp import McpToolExecutor, McpToolRuntimeReader
from domain.session import SessionRepository
from infrastructure.embedding import LocalEmbedding
from infrastructure.embedding.profile import embedding_profile_id
from infrastructure.knowledge import (
    JiebaChineseTokenizer,
    LocalKnowledgeTokenCounter,
)
from infrastructure.mcp.execution import McpToolCallClient
from infrastructure.memory import LocalReranker, LocalTokenCounter

logger = logging.getLogger(__name__)


def _env_flag(name: str, *, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name}_invalid")


def _env_csv_set(name: str) -> frozenset[str]:
    raw = os.getenv(name, "")
    if not raw.strip():
        return frozenset()
    parts = [part.strip() for part in raw.split(",")]
    if any(not part for part in parts):
        raise ValueError(f"{name}_invalid")
    return frozenset(parts)


def _profile_id(model: str, revision: str) -> str:
    return embedding_profile_id(model, revision)


@dataclass(slots=True)
class QueryServiceBundle:
    """Memory and knowledge services with their process-scoped model resources."""

    memory: MemoryService | None
    knowledge: KnowledgeService | None
    project_knowledge: ProjectKnowledgeService | None
    knowledge_repository: KnowledgeRepository | None
    knowledge_original_store: OriginalStore | None
    memory_enabled: bool
    knowledge_enabled: bool
    _startables: tuple[Any, ...]
    _closables: tuple[Any, ...]

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
    memory_enabled = memory_service is not None or _env_flag("ATHENA_MEMORY_ENABLED", default=False)
    knowledge_enabled = knowledge_service is not None or _env_flag(
        "ATHENA_KNOWLEDGE_ENABLED", default=False
    )
    startables: list[Any] = []
    closables: list[Any] = []
    memory = memory_service
    knowledge = knowledge_service
    knowledge_repository: KnowledgeRepository | None = None
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
        tokenizer_model = os.getenv("ATHENA_MEMORY_TOKENIZER", embedding_model)
        tokenizer_revision = os.getenv("ATHENA_MEMORY_TOKENIZER_REVISION", embedding_revision)
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
        counter = LocalTokenCounter(tokenizer_model, tokenizer_revision)
        memory = MemoryService(
            _memory_repository(sessions),
            embedding,
            reranker,
            counter,
            profile_id=_profile_id(embedding_model, embedding_revision),
            vector_min_score=float(os.getenv("ATHENA_MEMORY_VECTOR_MIN_SCORE", "0.70")),
            timeout_seconds=float(os.getenv("ATHENA_MEMORY_TIMEOUT_SECONDS", "60")),
            rerank_min_score=float(os.getenv("ATHENA_MEMORY_RERANK_MIN_SCORE", "0.30")),
            keyword_fallback_min_score=(
                float(os.environ["ATHENA_MEMORY_KEYWORD_FALLBACK_MIN_SCORE"])
                if "ATHENA_MEMORY_KEYWORD_FALLBACK_MIN_SCORE" in os.environ
                else None
            ),
        )
        startables.extend((embedding, reranker, counter))
        closables.extend((embedding, reranker, counter))
    if knowledge_enabled and knowledge is None:
        minio_originals = MinioOriginalStore.from_environment()
        knowledge_originals = minio_originals
        model_name = os.getenv("ATHENA_KNOWLEDGE_EMBEDDING_MODEL", "BAAI/bge-m3")
        model_revision = os.getenv(
            "ATHENA_KNOWLEDGE_EMBEDDING_REVISION",
            "5617a9f61b028005a4858fdac845db406aefb181",
        )
        embedding = LocalEmbedding(
            model_name,
            model_revision,
            os.getenv("ATHENA_KNOWLEDGE_EMBEDDING_DEVICE", "cpu"),
        )
        knowledge_counter = LocalKnowledgeTokenCounter(model_name, model_revision)
        knowledge_repository = KnowledgeRepository(sessions)
        knowledge = KnowledgeService(
            knowledge_repository,
            JiebaChineseTokenizer(),
            embedding,
            knowledge_originals,
            knowledge_counter,
            profile_id=_profile_id(model_name, model_revision),
        )
        startables.extend((minio_originals, knowledge_counter, embedding))
        closables.extend((minio_originals, embedding, knowledge_counter))
    elif knowledge is not None and knowledge_repository is None:
        knowledge_repository = KnowledgeRepository(sessions)
    project_knowledge = (
        None
        if not knowledge_enabled or knowledge is None or knowledge_repository is None
        else ProjectKnowledgeService(projects, knowledge_repository)
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
    )


def _memory_repository(sessions: async_sessionmaker[AsyncSession]) -> Any:
    from adapter.memory.repository import MemoryRepository

    return MemoryRepository(sessions)


def build_mcp_executor(
    *,
    cleanup_timeout_seconds: float = 10.0,
) -> McpToolCallClient:
    """Create the single-call MCP executor with the deployment transport limits."""
    allowed_private_hosts = _env_csv_set("ATHENA_MCP_ALLOWED_PRIVATE_HOSTS")
    stdio_names = _env_csv_set("ATHENA_MCP_STDIO_BASE_ENV_NAMES")
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
    )


__all__ = [
    "QueryServiceBundle",
    "build_mcp_executor",
    "build_query_services",
    "build_run_tool_runtime",
]
