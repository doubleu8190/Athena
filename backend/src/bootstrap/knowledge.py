"""知识库进程共享的远程 Embedding、Token 计数与统一对象存储构造。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from adapter.knowledge.storage import MinioOriginalStore, MinioStorageConfig
from infrastructure.embedding import (
    OpenAICompatibleEmbeddingClient,
    OpenAICompatibleEmbeddingConfig,
    TiktokenTokenCounter,
    remote_embedding_profile_id,
)
from infrastructure.knowledge import JiebaChineseTokenizer
from infrastructure.storage.minio_client import MinioObjectClient

# PostgreSQL knowledge_chunks.embedding 列固定为 Vector(1024)，启动阶段必须严格对齐。
KNOWLEDGE_VECTOR_DIMENSIONS = 1024


@dataclass(slots=True)
class KnowledgeModelResources:
    """API、Run Worker 与 Knowledge Worker 共用的无状态知识库文本与向量资源。"""

    embedding: OpenAICompatibleEmbeddingClient
    counter: TiktokenTokenCounter
    tokenizer: JiebaChineseTokenizer
    profile_id: str

    async def start_tokenizer(self) -> None:
        """在工作线程中幂等预热中文分词器，兼容未实现 warmup 的测试替身。"""
        warmup = getattr(self.tokenizer, "warmup", None)
        if callable(warmup):
            await asyncio.to_thread(warmup)


def knowledge_embedding_config() -> OpenAICompatibleEmbeddingConfig:
    """读取并校验远程 Embedding 配置，确保维度与 pgvector 1024 列约束一致。"""
    return OpenAICompatibleEmbeddingConfig.from_environment(
        expected_dimensions=KNOWLEDGE_VECTOR_DIMENSIONS,
    )


def validate_knowledge_embedding_configuration() -> OpenAICompatibleEmbeddingConfig:
    """在创建数据库或对象存储连接前快速校验远程 Embedding 配置并返回复用对象。"""
    return knowledge_embedding_config()


def build_knowledge_models(
    *,
    embedding_config: OpenAICompatibleEmbeddingConfig | None = None,
) -> KnowledgeModelResources:
    """按统一 ATHENA_EMBEDDING_* 环境变量构造无状态远程向量客户端、tiktoken 计数与分词器。

    调用方已预先校验时可直接传入 embedding_config 避免重复解析环境变量；
    profile_id 稳定地由模型名与维度生成，确保 API、Run Worker 与 Knowledge Worker 一致。
    """
    config = embedding_config if embedding_config is not None else knowledge_embedding_config()
    return KnowledgeModelResources(
        embedding=OpenAICompatibleEmbeddingClient(config),
        counter=TiktokenTokenCounter(),
        tokenizer=JiebaChineseTokenizer(),
        profile_id=remote_embedding_profile_id(config.model, config.dimensions),
    )


def build_knowledge_original_store(
    *,
    object_client: MinioObjectClient | None = None,
    storage_config: MinioStorageConfig | None = None,
) -> MinioOriginalStore:
    """构造基于统一 ATHENA_STORAGE_* 的知识库原件存储；支持复用共享客户端或预校验配置。"""
    if object_client is not None:
        return MinioOriginalStore.from_environment(object_client=object_client)
    if storage_config is not None:
        return MinioOriginalStore(storage_config)
    return MinioOriginalStore.from_environment()


def validate_knowledge_storage_configuration() -> MinioStorageConfig:
    """在创建数据库、Redis 或模型资源前快速校验对象存储配置并返回复用对象。"""
    return MinioStorageConfig.from_environment()


__all__ = [
    "KNOWLEDGE_VECTOR_DIMENSIONS",
    "KnowledgeModelResources",
    "build_knowledge_models",
    "build_knowledge_original_store",
    "knowledge_embedding_config",
    "validate_knowledge_embedding_configuration",
    "validate_knowledge_storage_configuration",
]
