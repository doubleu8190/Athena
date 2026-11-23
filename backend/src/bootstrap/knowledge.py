"""知识库进程共享的远程 Embedding、Token 计数与统一对象存储构造。"""

from __future__ import annotations

from dataclasses import dataclass

from adapter.knowledge.storage import MinioOriginalStore, MinioStorageConfig
from infrastructure.embedding import (
    OpenAICompatibleEmbeddingClient,
    OpenAICompatibleEmbeddingConfig,
    TiktokenTokenCounter,
    remote_embedding_profile_id,
)
from infrastructure.knowledge import JiebaChineseTokenizer

# PostgreSQL knowledge_chunks.embedding 列固定为 Vector(1024)，启动阶段必须严格对齐。
KNOWLEDGE_VECTOR_DIMENSIONS = 1024


@dataclass(slots=True)
class KnowledgeModelResources:
    """API、Run Worker 与 Knowledge Worker 共用的无状态知识库文本与向量资源。"""

    embedding: OpenAICompatibleEmbeddingClient
    counter: TiktokenTokenCounter
    tokenizer: JiebaChineseTokenizer
    profile_id: str


def knowledge_embedding_config() -> OpenAICompatibleEmbeddingConfig:
    """读取并校验远程 Embedding 配置，确保维度与 pgvector 1024 列约束一致。"""
    return OpenAICompatibleEmbeddingConfig.from_environment(
        expected_dimensions=KNOWLEDGE_VECTOR_DIMENSIONS,
    )


def validate_knowledge_embedding_configuration() -> None:
    """在创建数据库或对象存储连接前快速校验远程 Embedding 配置。"""
    knowledge_embedding_config()


def build_knowledge_models() -> KnowledgeModelResources:
    """按统一 ATHENA_EMBEDDING_* 环境变量构造无状态远程向量客户端、tiktoken 计数与分词器。

    profile_id 稳定地由模型名与维度生成，确保 API、Run Worker 与 Knowledge Worker
    写入与检索使用完全一致的向量档案标识。
    """
    config = knowledge_embedding_config()
    return KnowledgeModelResources(
        embedding=OpenAICompatibleEmbeddingClient(config),
        counter=TiktokenTokenCounter(),
        tokenizer=JiebaChineseTokenizer(),
        profile_id=remote_embedding_profile_id(config.model, config.dimensions),
    )


def build_knowledge_original_store() -> MinioOriginalStore:
    """构造基于统一 ATHENA_STORAGE_* 的知识库原件存储；配置缺失或非法时立即失败。"""
    return MinioOriginalStore.from_environment()


def validate_knowledge_storage_configuration() -> None:
    """在创建数据库、Redis 或模型资源前快速校验对象存储配置。"""
    MinioStorageConfig.from_environment()


__all__ = [
    "KNOWLEDGE_VECTOR_DIMENSIONS",
    "KnowledgeModelResources",
    "build_knowledge_models",
    "build_knowledge_original_store",
    "knowledge_embedding_config",
    "validate_knowledge_embedding_configuration",
    "validate_knowledge_storage_configuration",
]
