"""Knowledge Worker 进程的组合根：集中装配数据库、MinIO、模型与 Worker。"""

from __future__ import annotations

import os
from dataclasses import dataclass

from adapter.knowledge.repository import KnowledgeRepository
from adapter.knowledge.storage import MinioOriginalStore
from app.knowledge.worker import KnowledgeWorker
from bootstrap.config import DatabaseSettings, KnowledgeSettings
from bootstrap.database import build_database
from bootstrap.knowledge import (
    KnowledgeModelResources,
    build_knowledge_models,
    build_knowledge_original_store,
)
from infrastructure.database import Database
from infrastructure.knowledge.parsers import LocalDocumentParserFactory

# 隔离解析工厂与非隔离解析器共用同一 OCR 语言配置，避免两处配置漂移。
OCR_LANGUAGES = "ch"


@dataclass(slots=True)
class KnowledgeWorkerBundle:
    """Knowledge Worker 构造过程中已创建、需要由入口清理的资源。"""

    database: Database | None = None
    originals: MinioOriginalStore | None = None
    models: KnowledgeModelResources | None = None
    worker: KnowledgeWorker | None = None
    settings: KnowledgeSettings | None = None


def build_knowledge_worker_resources(bundle: KnowledgeWorkerBundle) -> None:
    """第一阶段：校验统一配置并装配数据库、原件存储与模型资源。

    存储配置与模型资源都在 Worker 构造前完成，失败时入口只清理 bundle 中
    已填充的字段，保持与原先入口一致的失败清理语义。
    """
    database_url = DatabaseSettings.url_from_environment()
    if not database_url:
        raise RuntimeError("knowledge_database_required")
    settings = KnowledgeSettings.from_environment()
    bundle.settings = settings
    if settings.ocr_cache_root:
        os.environ["PADDLE_PDX_CACHE_HOME"] = settings.ocr_cache_root
    bundle.database = build_database(database_url)
    # 知识库原件与远程 Embedding/Tiktoken 已无状态化，
    # 统一复用 ATHENA_STORAGE_* 与 ATHENA_EMBEDDING_* 配置。
    bundle.originals = build_knowledge_original_store()
    bundle.models = build_knowledge_models()


def build_knowledge_worker(bundle: KnowledgeWorkerBundle) -> int:
    """第二阶段：用已启动的资源构造 Worker 并返回消费协程数。"""
    database = bundle.database
    originals = bundle.originals
    models = bundle.models
    assert database is not None and originals is not None and models is not None
    settings = bundle.settings or KnowledgeSettings.from_environment()
    bundle.worker = KnowledgeWorker(
        KnowledgeRepository(database.session_factory),
        LocalDocumentParserFactory(ocr_languages=OCR_LANGUAGES),
        originals,
        models.embedding,
        models.tokenizer,
        models.counter,
        profile_id=models.profile_id,
        owner_concurrency=settings.owner_concurrency,
    )
    return settings.worker_concurrency


__all__ = [
    "OCR_LANGUAGES",
    "KnowledgeWorkerBundle",
    "build_knowledge_worker",
    "build_knowledge_worker_resources",
]
