"""Memory Worker 组合根：仅装配提取所需数据库、文本模型与嵌入模型。"""

from __future__ import annotations

import os
from dataclasses import dataclass

from adapter.llm import OpenAIChatCompletionsAdapter
from adapter.llm.repository import SqlAlchemyModelProfileRepository
from adapter.memory.repository import MemoryRepository
from app.memory.extraction import ExtractionLimits, MemoryExtractionService
from app.memory.worker import MemoryWorker, MemoryWorkerConfig
from bootstrap.config import memory_enabled_from_env
from bootstrap.database import build_database
from infrastructure.database import Database
from infrastructure.embedding import LocalEmbedding, LocalTokenCounter
from infrastructure.embedding.profile import embedding_profile_id
from infrastructure.security.mcp_secrets import McpSecretCipher


@dataclass(slots=True)
class MemoryWorkerBundle:
    """构造过程中已创建、需要由入口逆序清理的资源。"""

    database: Database | None = None
    provider: OpenAIChatCompletionsAdapter | None = None
    embedding: LocalEmbedding | None = None
    counter: LocalTokenCounter | None = None
    worker: MemoryWorker | None = None
    # 暴露已校验的运行配置，供入口直接复用轮询间隔等参数，避免重复读取环境变量。
    config: MemoryWorkerConfig | None = None


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    """读取有界整数配置，非法值在 Worker 启动时明确失败。"""
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as error:
        raise ValueError(f"{name.lower()}_invalid") from error
    if not minimum <= value <= maximum:
        raise ValueError(f"{name.lower()}_invalid")
    return value


def _bounded_float(name: str, default: float, minimum: float, maximum: float) -> float:
    """读取有界浮点配置，非法值在 Worker 启动时明确失败。"""
    raw = os.getenv(name, str(default)).strip()
    try:
        value = float(raw)
    except ValueError as error:
        raise ValueError(f"{name.lower()}_invalid") from error
    if not minimum <= value <= maximum:
        raise ValueError(f"{name.lower()}_invalid")
    return value


def build_memory_worker_dependencies(
    bundle: MemoryWorkerBundle,
    *,
    database_url: str | None = None,
) -> None:
    """按明确开关构造独立 Memory Worker，不加载 reranker。"""
    if not memory_enabled_from_env():
        raise RuntimeError("memory_worker_memory_disabled")
    configured_url = database_url or os.getenv("ATHENA_DATABASE_URL", "").strip()
    if not configured_url:
        raise RuntimeError("memory_worker_database_required")

    database = build_database(configured_url)
    bundle.database = database
    secret_cipher = McpSecretCipher.from_environment()
    profiles = SqlAlchemyModelProfileRepository(database.session_factory, secret_cipher)
    provider = OpenAIChatCompletionsAdapter()
    bundle.provider = provider
    repository = MemoryRepository(database.session_factory)

    embedding_model = os.getenv("ATHENA_MEMORY_EMBEDDING_MODEL", "BAAI/bge-m3")
    embedding_revision = os.getenv(
        "ATHENA_MEMORY_EMBEDDING_REVISION",
        "5617a9f61b028005a4858fdac845db406aefb181",
    )
    embedding = LocalEmbedding(
        embedding_model,
        embedding_revision,
        os.getenv("ATHENA_MEMORY_EMBEDDING_DEVICE", "cpu"),
    )
    bundle.embedding = embedding
    tokenizer_model = os.getenv("ATHENA_MEMORY_TOKENIZER", embedding_model)
    tokenizer_revision = os.getenv(
        "ATHENA_MEMORY_TOKENIZER_REVISION",
        embedding_revision,
    )
    counter = LocalTokenCounter(tokenizer_model, tokenizer_revision)
    bundle.counter = counter
    extraction = MemoryExtractionService(
        repository,
        provider,
        embedding,
        counter,
        embedding_profile_id=embedding_profile_id(
            embedding_model,
            embedding_revision,
        ),
        limits=ExtractionLimits(
            extraction_max_output_tokens=_bounded_int(
                "ATHENA_MEMORY_EXTRACTION_OUTPUT_TOKENS",
                4_000,
                128,
                100_000,
            ),
            decision_max_output_tokens=_bounded_int(
                "ATHENA_MEMORY_DECISION_OUTPUT_TOKENS",
                4_000,
                64,
                20_000,
            ),
            decision_context_max_tokens=_bounded_int(
                "ATHENA_MEMORY_DECISION_CONTEXT_TOKENS",
                8_000,
                512,
                200_000,
            ),
        ),
    )
    config = MemoryWorkerConfig(
        batch_run_limit=_bounded_int(
            "ATHENA_MEMORY_EXTRACT_BATCH_RUNS",
            20,
            1,
            20,
        ),
        batch_token_limit=_bounded_int(
            "ATHENA_MEMORY_EXTRACT_BATCH_TOKENS",
            12_000,
            1_000,
            1_000_000,
        ),
        fact_max_age_days=_bounded_int(
            "ATHENA_MEMORY_FACT_ARCHIVE_DAYS",
            180,
            1,
            36_500,
        ),
        other_max_age_days=_bounded_int(
            "ATHENA_MEMORY_OTHER_ARCHIVE_DAYS",
            365,
            1,
            36_500,
        ),
        idle_sleep_seconds=_bounded_float(
            "ATHENA_MEMORY_WORKER_POLL_SECONDS",
            1.0,
            0.05,
            60.0,
        ),
    )
    bundle.config = config
    bundle.worker = MemoryWorker(
        repository,
        profiles,
        extraction,
        config=config,
    )


__all__ = ["MemoryWorkerBundle", "build_memory_worker_dependencies"]
